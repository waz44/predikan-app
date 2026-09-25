"""
Modul: services.pipeline
Själva bearbetningspipelinen (klippning -> transkribering -> AI-berikning
-> Spreaker-publicering -> e-post) och den enda kö-arbetartråden som kör
den. Se services/state.py för det delade runtime-tillståndet och
modules/queue_store.py för hur kön/jobbstatus lagras beständigt.

STEG 2-6: Klippning -> Metadata -> Transkribering -> AI -> Spreaker -> E-post.
Läggs till i bearbetningskön via routers/process.py och routers/bulk_import.py,
och plockas upp härifrån (_queue_worker) så frontend kan polla verklig,
live status inklusive procentuell framdrift per steg.

Översikt över flödet för ett vanligt jobb:

    routers/process.py  --lägger till-->  queue_items (databasen)
                                              |
    queue_worker_loop  <--plockar nästa--------+
          |
          v
    _run_queue_item  ->  _run_processing_job  (klipp, transkribera, AI,
                                                publicera, e-post, historik)

"Generera om" i Hantera Spreaker går samma väg men kör
_run_regenerate_job i stället, som bara transkriberar och tar fram ett
nytt AI-förslag - den publicerar aldrig något.

Endast EN arbetartråd finns, så bara ett jobb körs åt gången. Det är
medvetet: transkribering och lokal AI tar i princip all datorkraft, och
två samtidiga jobb skulle bara göra båda långsammare.
"""
# re: städning av filnamn (se _sanitize_for_filename).
import re

# shutil: flytta eller kopiera originalfilen till uploads/.
import shutil

# tempfile: tillfällig mapp för ljud som laddas ner vid "Generera om".
import tempfile

# threading: avbrytssignaler (Event) och trådarna som driver procentmätarna.
import threading

# time: mäta hur lång tid bearbetningen tar (till statistiken).
import time

# datetime/timedelta: tidsstämplar i filnamn och "klar ca kl. X" i kön.
from datetime import datetime, timedelta

# Path: alla sökvägar hanteras som Path-objekt, oavsett operativsystem.
from pathlib import Path

# pydantic validerar fälten i en bearbetningsbegäran (se ProcessRequest).
from pydantic import BaseModel

# config: alla inställningar från .env (mappar, AI-leverantör m.m.).
import config

# Appens egna moduler - var och en ansvarar för ett steg eller en datakälla.
from modules import (
    ai_enrichment,  # titel, beskrivning och taggar via AI
    app_logging,  # gemensam loggfil (app.log)
    audio_processor,  # klippning, normalisering och ljudlängd
    db,  # SQLite-anslutningen
    email_notifier,  # bekräftelsemail efter publicering
    episode_store,  # historik och statistik över bearbetade predikningar
    podcast_archive,  # det lokala podd-arkivet (mp3 + transkript)
    queue_store,  # bearbetningskön i databasen
    spreaker_client,  # uppladdning och nedladdning mot Spreaker
    spreaker_episode_store,  # lokal kopia av avsnitten på Spreaker-kontot
    storage_cleanup,  # borttagning av filer för avbrutna/misslyckade jobb
    transcription,  # spara transkript till fil
    transcription_worker,  # kör transkriberingen i en avbrytbar bakgrundsprocess
)

# Delat tillstånd i minnet: pågående framsteg, avbrytssignaler m.m.
from services import state

# De steg som varje bearbetning går igenom, i ordning, samt hur stor andel
# (vikt) varje steg utgör av den totala, sammanvägda procentmätaren.
# Transkribering och Spreaker-uppladdning brukar ta längst tid, så de väger tyngst.
# Varje post är (nyckel, text som visas i kön, vikt). Vikterna summerar till 100.
# Nyckeln används i koden (t.ex. _set_step(progress, "trim", ...)) och texten
# visas som den är i kön i webbläsaren.
STEP_DEFS = [
    ("trim", "✂️ Klipper och normaliserar ljud", 10),
    ("transcription", "📝 Transkriberar predikan", 40),
    ("ai_enrichment", "🤖 AI genererar titel/beskrivning", 15),
    ("spreaker_publish", "📡 Publicerar på Spreaker", 30),
    ("email", "📧 Skickar bekräftelse", 5),
]

# Steg för "Generera om"-jobb (Hantera Spreaker-fliken, se _run_regenerate_job)
# - betydligt enklare än STEP_DEFS: ingen klippning/publicering/e-post,
# bara det som krävs för att komma fram till ett nytt AI-förslag.
# Transkriberingen väger tyngst eftersom den tar överlägset längst tid.
REGENERATE_STEP_DEFS = [
    ("download", "⬇️ Hämtar ljud från Spreaker", 15),
    ("transcription", "📝 Transkriberar", 55),
    ("ai_enrichment", "🤖 AI genererar", 30),
]


class ProcessRequest(BaseModel):
    """
    En begäran om att bearbeta en uppladdad predikan - det användaren fyllt
    i formuläret (eller en rad i en CSV-bulkimport). Sparas som "fields" i
    kön och återskapas härifrån när jobbet körs.

    pydantic kontrollerar typerna när objektet skapas: ett tal som kommer
    in som text ("12.5") görs om till float, och ett saknat obligatoriskt
    fält ger ett tydligt fel i stället för ett krångligt fel längre fram.
    """

    # Id för den uppladdade filen (se routers/upload.py). Köade jobb har
    # redan sin sökväg i databasen, så där är värdet bara "queue".
    file_id: str
    # Vilken del av ljudet som ska publiceras, i sekunder från början.
    start_seconds: float
    end_seconds: float
    # Talaren är obligatorisk - den skrivs alltid sist i beskrivningen.
    speaker: str
    # Tomt = låt AI:n skriva titel/beskrivning utifrån transkriptet.
    title: str = ""
    description: str = ""
    # Fritt fält som bara sparas i historiken (skickas inte till Spreaker).
    category: str = ""
    # Valfritt: "YYYY-MM-DDTHH:MM" (från ett <input type="datetime-local">).
    # Tomt = publicera direkt.
    publish_date: str = ""


def _new_progress(step_defs: list[tuple[str, str, int]] = STEP_DEFS) -> dict:
    """
    Nytt framstegsobjekt för ett jobb: alla steg "pending" på 0 %. Det hålls
    bara i minnet (state.RUNNING_PROGRESS) medan jobbet körs - det är ren
    visning i kön, inget som behöver överleva en omstart.

    Args:
        step_defs: Stegen som jobbet går igenom - STEP_DEFS för vanliga
            jobb, REGENERATE_STEP_DEFS för "Generera om".

    Returns:
        En dict med listan "steps" (ett objekt per steg) samt totalprocent
        och tidsuppskattning. Den ändras sedan på plats under körningen.
    """
    return {
        # Ett objekt per steg. "status" går pending -> running -> done
        # (eller skipped/error), och "percent" 0 -> 100.
        "steps": [
            {"key": k, "label": label, "status": "pending", "percent": 0, "weight": w}
            for k, label, w in step_defs
        ],
        # Viktat snitt av alla stegs procent (se _update_overall_percent).
        "overall_percent": 0,
        # Uppskattad total tid (sekunder) och när jobbet väntas bli klart,
        # baserat på tidigare bearbetningar (se episode_store).
        "estimated_seconds": None,
        "estimated_completion_at": None,
    }


def _set_step(progress: dict, key: str, status: str, percent: int | None = None) -> None:
    """
    Sätter status (och eventuellt procent) för ett steg och räknar om totalen.

    Args:
        progress: Jobbets framstegsobjekt (från _new_progress).
        key: Stegets nyckel, t.ex. "transcription".
        status: 'running' | 'done' | 'error' | 'skipped'
        percent: Ny procent för steget. Utelämnas den behålls nuvarande
            procent - utom för "done", som alltid blir 100.
    """
    # Leta upp rätt steg - listan är kort (3-5 steg), så en loop räcker.
    for step in progress["steps"]:
        if step["key"] == key:
            step["status"] = status
            if percent is not None:
                step["percent"] = percent
            elif status == "done":
                # Ett klart steg är alltid 100 %, även om tickern inte hann dit.
                step["percent"] = 100
            elif status in ("pending", "skipped", "error"):
                pass  # lämna percent som det är
            break
    _update_overall_percent(progress)


def _set_step_percent(progress: dict, key: str, percent: int) -> None:
    """
    Uppdaterar bara procenten för ett steg (begränsad till 0-100).

    Anropas ofta - av tickrarna flera gånger i sekunden och av Spreaker-
    uppladdningen för varje skickad bit - så statusen lämnas orörd.
    """
    for step in progress["steps"]:
        if step["key"] == key:
            # max/min skyddar mot värden utanför 0-100 från en ticker eller
            # uppladdningsräknare som räknat lite fel.
            step["percent"] = max(0, min(100, percent))
            break
    _update_overall_percent(progress)


def _update_overall_percent(progress: dict) -> None:
    """
    Räknar om totalprocenten som ett viktat snitt av stegen - ett steg med
    vikt 40 påverkar totalen fyra gånger så mycket som ett med vikt 10.

    Exempel: klippning klar (10 * 100) och transkribering halvvägs (40 * 50)
    ger (1000 + 2000) / 100 = 30 % totalt.
    """
    total_weight = sum(s["weight"] for s in progress["steps"])
    weighted_sum = sum(s["weight"] * s["percent"] for s in progress["steps"])
    # "if total_weight" skyddar mot division med noll om listan skulle vara tom.
    progress["overall_percent"] = int(weighted_sum / total_weight) if total_weight else 0


def _run_ticking_estimate(progress: dict, step_key: str, estimated_seconds: float, stop_event: threading.Event) -> None:
    """
    Simulerar en stigande procentandel för steg som inte kan rapportera
    verklig framdrift (t.ex. Whisper-transkribering eller ett AI-anrop).

    Procenten stiger mot 95% baserat på förfluten tid mot en uppskattad
    total tid. Om arbetet tar längre än uppskattat (vanligt om datorn är
    långsammare än gissningen, t.ex. lokal Whisper på CPU) fortsätter
    procenten krypa långsamt vidare mot 99% istället för att frysa helt -
    så det syns tydligt att arbetet fortfarande pågår, inte har fastnat.
    Det sista steget till 100% sätts av den faktiska koden när arbetet
    verkligen är klart.

    Körs i en egen daemon-tråd per steg och avslutas när stop_event sätts
    (vilket alltid görs i ett finally-block, så tråden aldrig blir kvar).

    Args:
        progress: Jobbets framstegsobjekt.
        step_key: Steget vars procent ska drivas framåt.
        estimated_seconds: Gissad tid för steget.
        stop_event: Sätts när steget är klart (eller har misslyckats).
    """
    start = time.time()
    # Minst 1 s, så att en uppskattning på 0 inte ger division med noll.
    estimated_seconds = max(1.0, estimated_seconds)
    while not stop_event.is_set():
        elapsed = time.time() - start
        if elapsed <= estimated_seconds:
            # Inom uppskattad tid: linjärt från 0 till 95 %.
            percent = int((elapsed / estimated_seconds) * 95)
        else:
            # Över tiden: 4 procentenheter till per "uppskattad tid" som gått,
            # men aldrig över 99 % - 100 % sätts bara när steget är klart.
            overtime = elapsed - estimated_seconds
            percent = min(99, 95 + int((overtime / estimated_seconds) * 4))
        _set_step_percent(progress, step_key, percent)
        # Uppdateras 2-3 gånger per sekund - kön pollas var 1,5 s, så det räcker gott.
        time.sleep(0.4)


def _sanitize_for_filename(s: str) -> str:
    """
    Ta bort/ersätt ogiltiga tecken för filnamn.

    Exempel: "Anna Andersson" -> "Anna-Andersson", "Pär Ölund" -> "Pr-lund".
    """
    s = s.strip()
    # Ersätt mellanslag med bindestreck
    s = re.sub(r"\s+", "-", s)
    # Behåll bara alfanumeriska, bindestreck, underscore och punkt
    # (å/ä/ö försvinner alltså - filnamnen i uploads/ och processed/ är
    # interna och behöver bara vara säkra på alla system, inte vackra).
    s = re.sub(r"[^A-Za-z0-9\-\._]", "", s)
    # Blev inget kvar (t.ex. ett namn med bara specialtecken) används "unknown".
    return s or "unknown"


def _format_publish_date_for_filename(publish_date: str) -> str:
    """
    Förväntar publish_date i formatet 'YYYY-MM-DDTHH:MM' eller tom sträng.
    Returnerar 'YYYYMMDD' eller 'nopub' om tomt/ogiltigt.

    Datumet i filnamnet gör det lätt att hitta rätt filer i processed/ i
    efterhand - "nopub" betyder att avsnittet publicerades direkt.
    """
    if not publish_date:
        return "nopub"
    try:
        dt = datetime.fromisoformat(publish_date)
        return dt.strftime("%Y%m%d")
    except Exception:
        # Fallback om användaren skickat bara 'YYYY-MM-DD'
        try:
            dt = datetime.fromisoformat(publish_date + "T00:00")
            return dt.strftime("%Y%m%d")
        except Exception:
            # Helt oläsbart datum - filnamnet får "nopub" i stället för att
            # hela jobbet stoppas av något som bara påverkar ett filnamn.
            return "nopub"


def queue_item_view(item: dict) -> dict:
    """
    Slår ihop en beständig queue_items-rad med live-progress (om jobbet just nu körs) till ett API-svar.

    Args:
        item: En rad ur kön (från queue_store), med status, resultat m.m.

    Returns:
        Den dict som skickas till webbläsaren för raden i kön.
    """
    # Bara det jobb som körs just nu har live-framsteg i minnet. För alla
    # andra används procenten som sparades i databasen när de blev klara.
    progress = state.RUNNING_PROGRESS.get(item["job_id"]) if item["status"] == "running" else None
    if progress:
        # Pågående jobb: aktuella siffror direkt från minnet.
        overall_percent = progress["overall_percent"]
        steps = progress["steps"]
        estimated_seconds = progress["estimated_seconds"]
        estimated_completion_at = progress["estimated_completion_at"]
    else:
        # Väntande eller avslutade jobb: ingen stegvy visas för dem.
        overall_percent = item["overall_percent"]
        steps = []
        estimated_seconds = None
        estimated_completion_at = None

    # Det här är exakt vad kön i frontend (static/app.js) får för varje rad.
    return {
        "queue_id": item["queue_id"],
        "job_id": item["job_id"],
        "kind": item["kind"],
        "filename": item["filename"],
        "speaker": item["speaker"],
        "status": item["status"],
        "overall_percent": overall_percent,
        "steps": steps,
        "estimated_seconds": estimated_seconds,
        "estimated_completion_at": estimated_completion_at,
        "error": item["error"],
        "result": item["result"],
    }


def _run_processing_job(
    job_id: str,
    req: ProcessRequest,
    original_path: Path,
    cancel_event: threading.Event,
    kind: str,
    keep_original: bool = False,
) -> None:
    """
    Kör själva bearbetningspipelinen. Detta körs i den enda
    kö-arbetartråden (se _queue_worker). Denna version flyttar originalfilen
    till uploads/ med ett beskrivande namn och sparar alla genererade filer
    i processed/ med samma basnamn.

    keep_original: om True KOPIERAS originalfilen in i uploads/ istället för
    att flyttas, så källfilen ligger orörd kvar där den låg (används av
    CSV-bulkimport, se _finish_bulk_item, som själv avgör om/när källfilen i
    BULK_IMPORT_DIR ska tas bort beroende på om jobbet lyckas eller inte).

    cancel_event: kollas vid varje steg-gräns (se _check_cancelled) - om
    satt avbryts jobbet innan nästa steg påbörjas. Under själva
    transkriberingssteget kollas den även löpande av
    modules/transcription_worker.py, som då dödar den separata
    transkriberingsprocessen på riktigt (se den modulens docstring).

    Args:
        job_id: Jobbets id i kön.
        req: Det användaren fyllt i (talare, klipp, titel m.m.).
        original_path: Den uppladdade (eller bulkimporterade) ljudfilen.
        cancel_event: Sätts när användaren klickar "Avbryt".
        kind: "manual" eller "bulk" - sparas i historiken.
        keep_original: Kopiera i stället för att flytta originalfilen.

    Funktionen returnerar inget. Resultatet (eller felet) sparas i kön via
    queue_store, där frontend läser det.
    """
    # Framstegsobjektet skapades av _run_queue_item innan anropet.
    progress = state.RUNNING_PROGRESS[job_id]
    job_start_time = time.time()
    # Längden på det klipp som faktiskt ska publiceras (minst 1 s, så
    # tidsuppskattningarna nedan aldrig blir noll).
    clip_duration = max(1.0, req.end_seconds - req.start_seconds)
    # Uppskattad total tid utifrån tidigare bearbetningar på samma dator
    # (None innan det finns någon historik) - visas som "klar ca kl. X" i kön.
    estimated_seconds = episode_store.estimate_processing_seconds(clip_duration)
    progress["estimated_seconds"] = estimated_seconds
    progress["estimated_completion_at"] = (
        (datetime.now() + timedelta(seconds=estimated_seconds)).isoformat()
        if estimated_seconds is not None
        else None
    )

    # ---- Bygg ett base-filenamn ----
    # Alla filer för jobbet får samma bas, t.ex.
    # "gudstjanst-Anna-Andersson-20260921-202609211030", följt av en ändelse
    # per filtyp (-clipped.mp3, -transcript.txt, -enrichment.json). Det gör
    # att filerna hör ihop och kan städas bort tillsammans.
    orig_stem = original_path.stem
    # Ändelsen behålls (i gemener) för originalfilen i uploads/.
    ext = original_path.suffix.lower()

    speaker_safe = _sanitize_for_filename(req.speaker)
    pubdate_part = _format_publish_date_for_filename(req.publish_date)
    # Tidsstämpeln gör namnet unikt även om samma fil bearbetas flera gånger.
    timestamp = datetime.now().strftime("%Y%m%d%H%M")
    orig_safe = _sanitize_for_filename(orig_stem)

    base_name = f"{orig_safe}-{speaker_safe}-{pubdate_part}-{timestamp}"

    # ---- Kopiera/flytta originalfilen till uploads/ under sitt basnamn ----
    try:
        new_upload_path = config.UPLOAD_DIR / f"{base_name}{ext}"
        if keep_original:
            # Bulkimport: källfilen i bulk_import/ får ligga kvar tills
            # jobbet har lyckats (se _finish_bulk_item).
            shutil.copy2(str(original_path), str(new_upload_path))
        else:
            # Flytta filen (behåll original om move misslyckas)
            shutil.move(str(original_path), str(new_upload_path))
        original_path = new_upload_path
        # Uppdatera UPLOADED_FILES mapping så frontend kan fortsätta spela filen
        for fid, p in list(state.UPLOADED_FILES.items()):
            # jämför Path lika
            if p == original_path or p.samefile(new_upload_path):
                state.UPLOADED_FILES[fid] = new_upload_path
                break
    except Exception:
        # Om något går fel med rename, fortsätt ändå med original_path
        pass

    # Spara basnamn och sökväg i databasen, så att filerna kan städas bort
    # om jobbet avbryts eller misslyckas (se _run_queue_item/_finish_bulk_item).
    queue_store.set_base_name_and_path(job_id, base_name, original_path)

    # --- STEG 2: Klipp & normalisera ---
    # Varje steg börjar med en kontroll om användaren har klickat "Avbryt".
    if _check_cancelled(job_id, cancel_event, progress):
        return
    _set_step(progress, "trim", "running", percent=0)
    # Klippningen tar ungefär 5 % av klippets längd (minst 2 s) - bara en
    # gissning för att procentmätaren ska röra sig medan ffmpeg arbetar.
    stop_event = threading.Event()
    ticker = threading.Thread(
        target=_run_ticking_estimate,
        args=(progress, "trim", max(2.0, clip_duration * 0.05), stop_event),
        daemon=True,
    )
    ticker.start()
    try:
        # Sätt filnamn för output i processed/ med base_name
        clipped_path = config.PROCESSED_DIR / f"{base_name}-clipped.mp3"
        # Klipper ut start-slut och jämnar ut ljudnivån. Resultatet är
        # alltid mp3, oavsett originalformat (wav, m4a, flac ...).
        audio_processor.trim_and_normalize(
            original_path, clipped_path, req.start_seconds, req.end_seconds
        )
    except Exception as exc:
        stop_event.set()
        _fail_job(job_id, f"Klippning misslyckades: {exc}", progress["overall_percent"])
        return
    finally:
        # Tickern stoppas alltid - både när steget lyckas och när det misslyckas.
        stop_event.set()
    _set_step(progress, "trim", "done")

    # --- STEG 4a: Transkribering ---
    # Körs i en separat bakgrundsprocess (modules/transcription_worker.py)
    # istället för direkt i denna tråd, så att den - det klart mest
    # tidskrävande steget - går att avbryta på riktigt (döda processen) om
    # cancel_event sätts medan den pågår.
    if _check_cancelled(job_id, cancel_event, progress):
        return
    _set_step(progress, "transcription", "running", percent=0)
    # Sekunder transkribering per sekund ljud: en inställning om den finns,
    # annars en tumregel (lokal Whisper på CPU är mycket långsammare än
    # OpenAI:s molntjänst). Påverkar bara procentmätaren, inte resultatet.
    transcription_factor = config.WHISPER_TIME_FACTOR or (1.8 if config.USE_LOCAL_WHISPER else 0.2)
    stop_event = threading.Event()
    ticker = threading.Thread(
        target=_run_ticking_estimate,
        args=(progress, "transcription", clip_duration * transcription_factor, stop_event),
        daemon=True,
    )
    ticker.start()
    try:
        # Transkriberar det KLIPPTA ljudet (inte originalet), så bara den
        # del som publiceras kommer med i transkriptet.
        transcript = transcription_worker.transcribe(clipped_path, config.BASE_DIR, cancel_event)
        # Spara transkriptionen till en textfil i processed/ med base_name
        transcript_path = config.PROCESSED_DIR / f"{base_name}-transcript.txt"
        transcription.save_transcript(transcript, transcript_path)
    except transcription_worker.TranscriptionCancelled as exc:
        # Användaren klickade "Avbryt" medan transkriberingen pågick.
        stop_event.set()
        _cancel_job(job_id, str(exc), progress["overall_percent"])
        return
    except Exception as exc:
        stop_event.set()
        _fail_job(job_id, f"Transkribering misslyckades: {exc}", progress["overall_percent"])
        return
    finally:
        stop_event.set()
    _set_step(progress, "transcription", "done")

    # --- STEG 4b: AI-berikning (tre separata anrop: titel, beskrivning, taggar) ---
    # Titel/beskrivning genereras bara om användaren lämnat fältet tomt -
    # mindre AI-belastning och bättre kontroll. Taggar genereras alltid,
    # eftersom formuläret inte har något manuellt tagg-alternativ.
    need_title = not req.title.strip()
    need_description = not req.description.strip()
    # Utgångsläge: det användaren själv skrivit (tomt om inget).
    final_title = req.title.strip()
    final_description = req.description.strip()
    tags: list[str] = []

    if _check_cancelled(job_id, cancel_event, progress):
        return
    _set_step(progress, "ai_enrichment", "running", percent=0)
    # Grov uppskattning per AI-anrop: en lokal modell via Ollama på CPU är
    # mycket långsammare än OpenAI. Bara för procentmätaren.
    estimated_per_call = 60.0 if config.AI_PROVIDER == "ollama" else 8.0
    calls_planned = 1 + int(need_title) + int(need_description)  # taggar körs alltid
    stop_event = threading.Event()
    ticker = threading.Thread(
        target=_run_ticking_estimate,
        args=(progress, "ai_enrichment", estimated_per_call * calls_planned, stop_event),
        daemon=True,
    )
    ticker.start()
    try:
        # Ordningen titel -> beskrivning -> taggar är medveten: alla tre
        # prompterna börjar med samma transkript, så Ollama bara behöver
        # läsa predikan i det första anropet (se ai_enrichment._SHARED_PREFIX).
        if need_title:
            final_title = ai_enrichment.generate_title(transcript, req.speaker, base_name=base_name)
        if need_description:
            final_description = ai_enrichment.generate_description(transcript, req.speaker, base_name=base_name)
        tags = ai_enrichment.generate_tags(transcript, req.speaker, base_name=base_name)
    except Exception as exc:
        # Typiska orsaker: Ollama körs inte, fel modellnamn, saknad OpenAI-nyckel.
        stop_event.set()
        _fail_job(job_id, f"AI-berikning misslyckades: {exc}", progress["overall_percent"])
        return
    finally:
        stop_event.set()
    _set_step(progress, "ai_enrichment", "done")

    # Spreaker kräver en titel - om AI:n inte gav någon används en reservtitel.
    if not final_title:
        final_title = f"Predikan av {req.speaker}"

    # Markerar avsnitt där AI:n inte kunde sammanfatta transkriptet (för
    # kort, obegripligt m.m.) - sparas i JSON-filen så de går att hitta.
    quality_flag = final_description.strip() == ai_enrichment.QUALITY_FALLBACK_TEXT

    # Lägg alltid till talaren som en egen rad sist i beskrivningen (oavsett
    # om den är AI-genererad eller manuellt ifylld), så den syns i den
    # publicerade beskrivningen på Spreaker och i bekräftelsemailet.
    final_description = (
        f"{final_description}\n\nTalare: {req.speaker}" if final_description else f"Talare: {req.speaker}"
    )

    # Spara resultatet (AI-genererat och/eller manuellt ifyllt) till JSON i processed/
    enrichment_result = {
        "title": final_title,
        "description": final_description,
        "tags": tags,
        "speaker": req.speaker,
        "quality_flag": quality_flag,
    }
    json_path = config.PROCESSED_DIR / f"{base_name}-enrichment.json"
    ai_enrichment.save_enrichment_result(enrichment_result, json_path)

    # --- STEG 5: Publicera på Spreaker (verklig uppladdningsprocent) ---
    # Ingen cancel-koll härefter - när avsnittet väl är publicerat går det
    # inte att ångra, så det är för sent att avbryta på ett meningsfullt sätt.
    if _check_cancelled(job_id, cancel_event, progress):
        return
    _set_step(progress, "spreaker_publish", "running", percent=0)

    # Uppladdningen rapporterar verklig procent (hur mycket av filen som
    # skickats), så här behövs ingen gissande ticker.
    def _on_upload_progress(percent: int) -> None:
        _set_step_percent(progress, "spreaker_publish", percent)

    try:
        # Med SPREAKER_SIMULATE=true simuleras uppladdningen (inget skickas).
        # Ett publiceringsdatum i framtiden schemalägger avsnittet, ett i
        # dåtiden bakåtdaterar det (se spreaker_client.publish_episode).
        publish_result = spreaker_client.publish_episode(
            audio_path=clipped_path,
            title=final_title,
            description=final_description,
            tags=tags,
            publish_date=req.publish_date,
            progress_callback=_on_upload_progress,
        )
    except Exception as exc:
        _fail_job(job_id, f"Publicering på Spreaker misslyckades: {exc}", progress["overall_percent"])
        return
    _set_step(progress, "spreaker_publish", "done")

    # Länken till det publicerade avsnittet visas i kön och i e-posten.
    episode_url = publish_result["episode_url"]

    # --- STEG 6: Bekräftelse via e-post (eller sammanfattningssida i UI) ---
    # Ett e-postfel fäller INTE jobbet: avsnittet är redan publicerat, och
    # resultatet syns ändå i kön. Felet loggas och steget markeras som fel.
    _set_step(progress, "email", "running", percent=0)
    email_sent = False
    try:
        email_sent = email_notifier.send_publish_confirmation(
            title=final_title,
            speaker=req.speaker,
            description=final_description,
            episode_url=episode_url,
            tags=tags,
            processing_seconds=time.time() - job_start_time,
        )
        # "skipped" när e-post är avstängt i inställningarna.
        _set_step(progress, "email", "done" if email_sent else "skipped", percent=100)
    except Exception as exc:
        _set_step(progress, "email", "error")
        app_logging.logger.error(f"E-postbekräftelse misslyckades för '{final_title}': {exc}")

    # --- Episodhistorik, statistik & lagringsstädning ---
    # Räknas bara för lyckade bearbetningar (annars snedvrids snittet av
    # avbrutna/misslyckade körningar).
    processing_seconds = time.time() - job_start_time
    created_at = datetime.now().isoformat()

    # Resultatet som visas i kön när jobbet är klart (länk, titel, taggar...).
    result = {
        "final_title": final_title,
        "final_description": final_description,
        "tags": tags,
        "episode_url": episode_url,
        # simulated/scheduled/backdated styr vilken text kön visar, t.ex.
        # "Schemalagd till ..." eller "(simulerad)".
        "simulated": publish_result.get("simulated", False),
        "scheduled": publish_result.get("scheduled", False),
        "backdated": publish_result.get("backdated", False),
        "publish_date": req.publish_date,
        "email_sent": email_sent,
        "speaker": req.speaker,
        "category": req.category,
        "processing_seconds": processing_seconds,
        # Var filerna för jobbet hamnade, för felsökning.
        "files": {
            "audio_mp3": str(clipped_path),
            "transcript_txt": str(transcript_path),
            "enrichment_json": str(json_path),
        }
    }

    # En rad i episodhistoriken: underlag för statistiken och tidsuppskattningen
    # (predikans längd mot bearbetningstid) och för lagringsstädningen nedan.
    episode_store.record_episode({
        "base_name": base_name,
        "speaker": req.speaker,
        "title": final_title,
        "description": final_description,
        "tags": tags,
        "publish_date": req.publish_date,
        "category": req.category,
        "kind": kind,
        "episode_url": episode_url,
        "simulated": result["simulated"],
        "scheduled": result["scheduled"],
        "backdated": result["backdated"],
        "email_sent": email_sent,
        "audio_path": str(clipped_path),
        "transcript_path": str(transcript_path),
        "enrichment_path": str(json_path),
        "sermon_seconds": clip_duration,
        "processing_seconds": processing_seconds,
        "created_at": created_at,
    })

    # Om MAX_STORED_EPISODES är satt: ta bort de äldsta predikningarnas filer.
    removed_bases = episode_store.enforce_retention(config.PROCESSED_DIR, config.MAX_STORED_EPISODES)
    if removed_bases:
        # Glöm även borttagna filer i minnet, så att vågformen inte försöker
        # spela upp en fil som inte längre finns.
        for fid, p in list(state.UPLOADED_FILES.items()):
            if p.stem in removed_bases:
                state.UPLOADED_FILES.pop(fid, None)

    # Sist: markera jobbet som klart. Kön visar då resultatet ovan.
    queue_store.set_finished(job_id, "done", result=result, overall_percent=100)


def _fail_job(job_id: str, message: str, overall_percent: int = 0) -> None:
    """
    Markerar jobbet som misslyckat, med ett felmeddelande som visas i kön.

    overall_percent sparas så att kön visar hur långt jobbet kom innan felet.
    """
    queue_store.set_finished(job_id, "error", error=message, overall_percent=overall_percent)


def _cancel_job(job_id: str, message: str, overall_percent: int = 0) -> None:
    """Markerar jobbet som avbrutet av användaren (visas som "Avbruten" i kön)."""
    queue_store.set_finished(job_id, "cancelled", error=message, overall_percent=overall_percent)


def _check_cancelled(job_id: str, cancel_event: threading.Event, progress: dict) -> bool:
    """
    Kollar cancel_event vid en steg-gräns. Returnerar True (och avbryter jobbet) om avbrytning begärts.

    Anroparen ska då returnera direkt, så att nästa steg aldrig påbörjas.
    """
    if cancel_event.is_set():
        _cancel_job(job_id, "Avbruten av användaren.", progress["overall_percent"])
        return True
    return False


def _run_queue_item(item: dict) -> None:
    """
    Kör ett enskilt köobjekt (manuellt, CSV-bulkimport, eller "Generera
    om" för ett befintligt Spreaker-avsnitt). Den sistnämnda kindens
    jobb (kind == "regenerate") har en helt egen, mycket enklare
    pipeline (_run_regenerate_job) - ingen klippning/publicering/e-post
    är relevant där. För bulkimport-objekt görs filkontroll och
    ljudlängd-uppslagning här, precis innan bearbetningen startar - se
    kommentaren vid _finish_bulk_item.

    Args:
        item: Raden ur kön, med kind ("manual", "bulk" eller "regenerate"),
            fields (formulärets värden) och sökvägen till ljudfilen.
    """
    job_id = item["job_id"]

    # "Generera om" har ett helt eget, kortare flöde.
    if item["kind"] == "regenerate":
        _run_regenerate_job(item)
        return

    if item["kind"] == "bulk":
        source_path = item["original_path"]
        filename = item["filename"]

        # Skyddsnät mot path traversal (se routers/bulk_import.py, som
        # redan avvisar filnamn med "/" eller "\" vid import) - dubbelkollas
        # här direkt innan filen faktiskt rörs.
        try:
            source_path.resolve().relative_to(config.BULK_IMPORT_DIR.resolve())
        except ValueError:
            message = "Ogiltig sökväg för bulkimport-filen - ligger utanför BULK_IMPORT_DIR."
            _fail_job(job_id, message)
            app_logging.logger.error(f"Bulkimport: '{filename}' avvisad - {message}")
            return

        # Filen kontrolleras först nu, inte vid import: en CSV kan köas långt
        # innan den bearbetas, och filen kan ha hunnit tas bort under tiden.
        if not source_path.exists():
            message = (
                f"Filen hittades inte i {config.BULK_IMPORT_DIR.name}/ - redan importerad "
                "och borttagen vid en tidigare körning, eller felstavat filnamn i CSV:n?"
            )
            _fail_job(job_id, message)
            app_logging.logger.warning(f"Bulkimport: '{filename}' hoppades över - {message}")
            return

        # Bulkimport klipper inte - hela filen publiceras, så slutpunkten är
        # filens längd. Den läses här och sparas även i databasen.
        try:
            duration = audio_processor.get_audio_duration_seconds(source_path)
            item["fields"]["end_seconds"] = duration
            queue_store.set_end_seconds(job_id, duration)
        except Exception as exc:
            # Trasig eller okänd ljudfil - jobbet kan inte fortsätta.
            _fail_job(job_id, f"Kunde inte läsa ljudfilen: {exc}")
            app_logging.logger.error(f"Bulkimport: '{filename}' misslyckades - kunde inte läsa ljudfilen: {exc}")
            return

        app_logging.logger.info(f"Bulkimport: bearbetar '{filename}' (talare: {item['speaker']})")

    # Registrera jobbet som pågående och lägg upp dess framsteg och
    # avbrytssignal i minnet, där kön och "Avbryt"-knappen hittar dem.
    queue_store.set_running(job_id)
    state.RUNNING_PROGRESS[job_id] = _new_progress()
    cancel_event = threading.Event()
    state.CANCEL_EVENTS[job_id] = cancel_event
    try:
        # Återskapa begäran från fälten som sparades när jobbet köades.
        req = ProcessRequest(file_id="queue", **item["fields"])
        _run_processing_job(
            job_id, req, item["original_path"], cancel_event, item["kind"], keep_original=item["keep_original"]
        )
    finally:
        # Städa alltid bort minnesposterna, även om jobbet kraschade.
        state.CANCEL_EVENTS.pop(job_id, None)
        state.RUNNING_PROGRESS.pop(job_id, None)

    # Läs tillbaka hur jobbet slutade, för att städa rätt filer nedan.
    job = queue_store.get_by_job_id(job_id)
    assert job is not None, "jobbet vi själva just körde klart måste finnas i databasen"
    if item["kind"] == "bulk":
        _finish_bulk_item(item, job)
    elif job["status"] == "cancelled":
        # Originalfilen i uploads/ lämnas orörd (kan vara användarens enda
        # kopia) - bara de ofärdiga resultatfilerna i processed/ städas bort.
        base_name = job.get("base_name")
        if base_name:
            storage_cleanup.delete_processed_files(config.PROCESSED_DIR, base_name)


def _archive_transcript(episode_id: int, transcript: str, only_if_missing: bool = False) -> None:
    """
    Sparar transkriptet i podd-arkivet om avsnittet finns där. Ett fel här
    (t.ex. en urkopplad arkivdisk) får aldrig fälla själva jobbet -
    transkriptet finns ju redan kvar i databasen.

    Args:
        episode_id: Spreakers id för avsnittet.
        transcript: Hela transkriptet.
        only_if_missing: True = skriv inte över ett transkript som redan
            finns i arkivet (används när det kommer från databasen).
    """
    try:
        if only_if_missing and podcast_archive.local_info(episode_id)["has_transcript"]:
            return
        podcast_archive.save_transcript(episode_id, transcript)
    except OSError as exc:
        app_logging.logger.warning(f"Kunde inte spara transkript för avsnitt {episode_id} i arkivet: {exc}")


def _run_regenerate_job(item: dict) -> None:
    """
    Genererar ett NYTT AI-förslag på titel och/eller beskrivning för ett
    REDAN publicerat Spreaker-avsnitt (Hantera Spreaker-fliken). Sparar
    ALDRIG till Spreaker själv - resultatet läggs bara i job["result"] för
    att frontend ska kunna fylla i redigeringsfälten och låta användaren
    granska/spara via det redan befintliga PUT-flödet
    (routers/spreaker_episodes.py:update_episode).

    Återanvänder transcription_worker/ai_enrichment rakt av, men är
    annars en betydligt enklare pipeline än _run_processing_job: ingen
    klippning, ingen Spreaker-publicering, ingen e-post. Om ett transkript
    redan finns cachat sen tidigare (modules/spreaker_episode_store.get_transcript)
    och omtranskribering inte begärts, hoppas nedladdning+transkribering
    över helt - det är den absolut mest tidskrävande delen, och samma
    avsnitts ljud ändras normalt aldrig.

    Det lokala podd-arkivet (modules/podcast_archive.py) används också:
    ett transkript som sparats där räcker på samma sätt som databascachen,
    och finns avsnittets mp3 i arkivet transkriberas den direkt i stället
    för att laddas ner från Spreaker. Ett nytt transkript sparas både i
    databasen och i arkivet (om avsnittet finns där).

    Args:
        item: Raden ur kön. item["fields"] innehåller episode_id och vilka
            fält som ska genereras om (se routers/spreaker_episodes.py).
    """
    job_id = item["job_id"]
    fields = item["fields"]
    # Fälten sattes av routers/spreaker_episodes.py:regenerate_episode.
    episode_id = fields["episode_id"]
    want_title = fields["regenerate_title"]
    want_description = fields["regenerate_description"]
    # Kryssrutan "Transkribera om": tvingar fram en ny transkribering även
    # om ett sparat transkript finns (t.ex. efter byte till en bättre modell).
    force_retranscribe = fields["force_retranscribe"]

    # Samma registrering som för vanliga jobb, men med de kortare stegen.
    queue_store.set_running(job_id)
    progress = _new_progress(step_defs=REGENERATE_STEP_DEFS)
    state.RUNNING_PROGRESS[job_id] = progress
    cancel_event = threading.Event()
    state.CANCEL_EVENTS[job_id] = cancel_event

    try:
        # Leta efter ett befintligt transkript: först i databasen, sedan i
        # arkivet. Det som hittas på ena stället kopieras till det andra, så
        # båda hålls i synk.
        cached_transcript = None
        if not force_retranscribe:
            cached_transcript = spreaker_episode_store.get_transcript(episode_id)
            if cached_transcript:
                _archive_transcript(episode_id, cached_transcript, only_if_missing=True)
            else:
                cached_transcript = podcast_archive.read_transcript(episode_id)
                if cached_transcript:
                    spreaker_episode_store.save_transcript(episode_id, cached_transcript)
        if cached_transcript:
            # Inget att ladda ner eller transkribera - gå direkt till AI:n.
            _set_step(progress, "download", "skipped", percent=100)
            _set_step(progress, "transcription", "skipped", percent=100)
            transcript = cached_transcript
        else:
            if _check_cancelled(job_id, cancel_event, progress):
                return
            _set_step(progress, "download", "running", percent=0)
            # Ticker för transkriberingssteget (samma mönster som
            # _run_processing_job). Utan den står steget kvar på 0% under hela
            # transkriberingen - som för en hel nedladdad predikan med lokal
            # Whisper på CPU kan ta många minuter - och ser felaktigt ut som om
            # inget händer. Startas efter nedladdningen och stoppas i finally.
            transcription_stop = threading.Event()
            try:
                # En tillfällig mapp för ljud som laddas ner från Spreaker -
                # den och filen i den tas bort automatiskt när blocket är klart.
                with tempfile.TemporaryDirectory(prefix="spreaker-regen-") as tmp_dir:
                    archived_audio = podcast_archive.audio_path(episode_id)
                    if archived_audio:
                        # Läses bara (aldrig flyttas/raderas) - arkivfilen ligger kvar.
                        audio_path = archived_audio
                        _set_step(progress, "download", "skipped", percent=100)
                    else:
                        # Inte arkiverat: hämta ljudet från Spreaker (kräver token).
                        audio_path = Path(tmp_dir) / f"{episode_id}.mp3"
                        spreaker_client.download_episode_audio(episode_id, audio_path)
                        _set_step(progress, "download", "done")

                    if _check_cancelled(job_id, cancel_event, progress):
                        return
                    _set_step(progress, "transcription", "running", percent=0)
                    # Ljudets längd behövs för att gissa hur lång tid
                    # transkriberingen tar (bara för procentmätaren).
                    try:
                        audio_seconds = audio_processor.get_audio_duration_seconds(audio_path)
                    except Exception:
                        audio_seconds = 600.0  # rimlig fallback om längden inte går att läsa
                    transcription_factor = config.WHISPER_TIME_FACTOR or (1.8 if config.USE_LOCAL_WHISPER else 0.2)
                    threading.Thread(
                        target=_run_ticking_estimate,
                        args=(progress, "transcription", audio_seconds * transcription_factor, transcription_stop),
                        daemon=True,
                    ).start()
                    transcript = transcription_worker.transcribe(audio_path, config.BASE_DIR, cancel_event)
            except transcription_worker.TranscriptionCancelled as exc:
                _cancel_job(job_id, str(exc), progress["overall_percent"])
                return
            except Exception as exc:
                _fail_job(job_id, f"Nedladdning/transkribering misslyckades: {exc}", progress["overall_percent"])
                return
            finally:
                transcription_stop.set()
            _set_step(progress, "transcription", "done")
            # Spara det nya transkriptet på båda ställena, så nästa "Generera
            # om" för samma avsnitt slipper transkribera igen.
            spreaker_episode_store.save_transcript(episode_id, transcript)
            _archive_transcript(episode_id, transcript)

        if _check_cancelled(job_id, cancel_event, progress):
            return
        _set_step(progress, "ai_enrichment", "running", percent=0)
        # Talaren hämtas från den lokala avsnittslistan (utläst ur
        # beskrivningens "Talare:"-rad) - behövs både i prompten och sist
        # i en ny beskrivning.
        cached = spreaker_episode_store.get(episode_id)
        speaker = (cached or {}).get("speaker") or ""
        # Basnamn för AI:ns felsökningsfiler i processed/.
        base_name = f"spreaker-regen-{episode_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        new_title = None
        new_description = None
        try:
            if want_title:
                new_title = ai_enrichment.generate_title(transcript, speaker, base_name=base_name)
            if want_description:
                new_description = ai_enrichment.generate_description(transcript, speaker, base_name=base_name)
                # Samma konvention som _run_processing_job använder för
                # NYA avsnitt: talaren som en egen rad sist i beskrivningen
                # - annars tappar man den raden (och därmed Talare-kolumnen
                # i Hantera Spreaker-tabellen, som läser ut den därifrån)
                # så fort ett regenererat förslag sparas.
                if speaker:
                    new_description = f"{new_description}\n\nTalare: {speaker}" if new_description else f"Talare: {speaker}"
        except Exception as exc:
            _fail_job(job_id, f"AI-generering misslyckades: {exc}", progress["overall_percent"])
            return
        _set_step(progress, "ai_enrichment", "done")

        # Bara de fält som begärts har ett värde; det andra är None, så att
        # frontend vet vilket fält som ska få ett förslag.
        result = {"episode_id": episode_id, "title": new_title, "description": new_description}
        queue_store.set_finished(job_id, "done", result=result, overall_percent=100)
    finally:
        # Städa alltid bort minnesposterna, oavsett hur jobbet slutade.
        state.CANCEL_EVENTS.pop(job_id, None)
        state.RUNNING_PROGRESS.pop(job_id, None)


def _finish_bulk_item(item: dict, job: dict) -> None:
    """
    Städning specifik för CSV-bulkimport (se routers/bulk_import.py):
    källfilen i BULK_IMPORT_DIR tas bort bara vid lyckad publicering.
    Misslyckas jobbet lämnas källfilen orörd, men halvfärdiga filer i
    uploads/+processed/ för detta försök städas bort, så en ny körning av
    samma CSV inte lämnar skräp efter sig.

    Args:
        item: Raden ur kön (med sökvägen till källfilen).
        job: Samma rad läst efter körningen (med status och ev. fel).
    """
    filename = item["filename"]
    source_path = item["original_path"]

    if job["status"] == "done":
        try:
            source_path.unlink()
        except OSError as exc:
            # Publiceringen lyckades ändå - att filen inte gick att ta bort
            # (t.ex. öppen i ett annat program) är bara en varning.
            app_logging.logger.warning(
                f"Bulkimport: '{filename}' publicerad, men kunde inte tas bort "
                f"från {config.BULK_IMPORT_DIR.name}/: {exc}"
            )
        else:
            app_logging.logger.info(
                f"Bulkimport: '{filename}' publicerad och borttagen från {config.BULK_IMPORT_DIR.name}/"
            )
    else:
        # Misslyckat eller avbrutet: ta bort kopian i uploads/ och de
        # halvfärdiga filerna i processed/ - källfilen ligger kvar.
        base_name = job.get("base_name")
        if base_name:
            storage_cleanup.delete_episode_files(config.UPLOAD_DIR, config.PROCESSED_DIR, base_name)
        app_logging.logger.error(
            f"Bulkimport: '{filename}' misslyckades - {job.get('error')}. "
            f"Filen ligger kvar i {config.BULK_IMPORT_DIR.name}/ för en ny körning."
        )


def queue_worker_loop(database_file, stop_event: threading.Event) -> None:
    """
    Enda bakgrundsarbetaren för hela bearbetningskön. Kör i en evighetsloop
    i en egen daemon-tråd, startad av app.py vid uppstart och stoppad via
    stop_event av app.py:s shutdown-hook. Plockar bara upp ett NYTT objekt
    när kön inte är pausad (modules/queue_store.py:next_queued_unless_paused(),
    en enda atomär fråga - se dess docstring för varför paus-kollen och
    hämtningen inte får vara två separata anrop) - ett redan påbörjat
    objekt får alltid bli klart innan loopen tittar på pausläget igen.

    database_file och stop_event fångas/skapas av app.py:_on_startup i
    HUVUDTRÅDEN, precis innan DENNA tråd startas, och skickas in explicit
    hit istället för att läsas/delas via en global. Två separata skäl:

    - database_file: se modules/db.py:bind_thread_to_database_file - i
      korthet, om tråden själv läste config.DATABASE_FILE skulle det finnas
      ett fönster mellan threading.Thread.start() och att tråden faktiskt
      hinner schemaläggas och köra denna rad, under vilket värdet kan hinna
      ändras (t.ex. mellan pytest-tester, under belastning som gör
      OS-schemaläggning långsammare).

    - stop_event: en EGEN Event per arbetartråd (istället för en delad
      global som rensas med .clear() vid varje ny appstart) gör att bara
      DENNA trådens egen _on_shutdown någonsin kan stoppa/återuppliva den.
      Med en delad global skulle en tråd vars join(timeout=...) i
      _on_shutdown hann ge upp INNAN tråden faktiskt avslutat sig kunna
      "återupplivas" av en SENARE appstart som rensar samma globala Event -
      och då fortsätta loopa mot ett tredje testfalls tillstånd.
    """
    # Fäst tråden vid databasfilen som gällde när den startades.
    db.bind_thread_to_database_file(database_file)
    while not stop_event.is_set():
        # Hämtar nästa väntande objekt - eller None om kön är tom/pausad.
        item = queue_store.next_queued_unless_paused()
        if item:
            # Kön i frontend markerar vilket objekt som körs just nu.
            state.set_current_queue_id(item["queue_id"])

        if item is None:
            # Inget att göra (tom eller pausad kö) - vänta en halv sekund.
            # wait() i stället för sleep(), så att en nedstängning väcker
            # tråden direkt i stället för att vänta ut tiden.
            stop_event.wait(0.5)
            continue

        try:
            _run_queue_item(item)
        finally:
            # Oavsett resultat körs inget objekt längre.
            state.set_current_queue_id(None)
