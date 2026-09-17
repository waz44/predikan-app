"""
Predikan-till-Podcast - huvudapplikation (FastAPI)

Kör lokalt med:
    uvicorn app:app --reload

Se README.md för fullständiga instruktioner.
"""
import time
import threading
import uuid
import shutil
import csv
import io
from pathlib import Path

import re
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from modules import (
    audio_processor,
    transcription,
    ai_enrichment,
    spreaker_client,
    email_notifier,
    storage_cleanup,
    stats,
)

app = FastAPI(title="Predikan → Podcast")

# Håller reda på uppladdade original-filer under körningens gång (file_id -> path)
UPLOADED_FILES: dict[str, Path] = {}

# Håller reda på pågående/klara bearbetningsjobb (job_id -> status-dict)
JOBS: dict[str, dict] = {}

# Håller reda på pågående/klara CSV-bulkimporter (batch_id -> status-dict)
BATCHES: dict[str, dict] = {}

# De steg som varje bearbetning går igenom, i ordning, samt hur stor andel
# (vikt) varje steg utgör av den totala, sammanvägda procentmätaren.
# Transkribering och Spreaker-uppladdning brukar ta längst tid, så de väger tyngst.
STEP_DEFS = [
    ("trim", "✂️ Klipper och normaliserar ljud", 10),
    ("transcription", "📝 Transkriberar predikan", 40),
    ("ai_enrichment", "🤖 AI genererar titel/beskrivning", 15),
    ("spreaker_publish", "📡 Publicerar på Spreaker", 30),
    ("email", "📧 Skickar bekräftelse", 5),
]


def _new_job() -> dict:
    return {
        "status": "running",  # running | done | error
        "steps": [
            {"key": k, "label": l, "status": "pending", "percent": 0, "weight": w}
            for k, l, w in STEP_DEFS
        ],
        "overall_percent": 0,
        "estimated_seconds": None,
        "result": None,
        "error": None,
    }


def _set_step(job: dict, key: str, status: str, percent: int | None = None) -> None:
    """status: 'running' | 'done' | 'error' | 'skipped'"""
    for step in job["steps"]:
        if step["key"] == key:
            step["status"] = status
            if percent is not None:
                step["percent"] = percent
            elif status == "done":
                step["percent"] = 100
            elif status in ("pending", "skipped", "error"):
                pass  # lämna percent som det är
            break
    _update_overall_percent(job)


def _set_step_percent(job: dict, key: str, percent: int) -> None:
    for step in job["steps"]:
        if step["key"] == key:
            step["percent"] = max(0, min(100, percent))
            break
    _update_overall_percent(job)


def _update_overall_percent(job: dict) -> None:
    total_weight = sum(s["weight"] for s in job["steps"])
    weighted_sum = sum(s["weight"] * s["percent"] for s in job["steps"])
    job["overall_percent"] = int(weighted_sum / total_weight) if total_weight else 0


def _run_ticking_estimate(job: dict, step_key: str, estimated_seconds: float, stop_event: threading.Event) -> None:
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
    """
    start = time.time()
    estimated_seconds = max(1.0, estimated_seconds)
    while not stop_event.is_set():
        elapsed = time.time() - start
        if elapsed <= estimated_seconds:
            percent = int((elapsed / estimated_seconds) * 95)
        else:
            overtime = elapsed - estimated_seconds
            percent = min(99, 95 + int((overtime / estimated_seconds) * 4))
        _set_step_percent(job, step_key, percent)
        time.sleep(0.4)


# ---------------------------------------------------------------------------
# STEG 1: Uppladdning
# ---------------------------------------------------------------------------
@app.post("/api/upload")
async def upload_audio(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Filtyp '{ext}' stöds ej. Tillåtna format: {config.ALLOWED_EXTENSIONS}",
        )

    file_id = str(uuid.uuid4())
    dest_path = config.UPLOAD_DIR / f"{file_id}{ext}"

    with dest_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    UPLOADED_FILES[file_id] = dest_path

    try:
        duration = audio_processor.get_audio_duration_seconds(dest_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Kunde inte läsa ljudfilen: {exc}")

    return {"file_id": file_id, "filename": file.filename, "duration_seconds": duration}


@app.get("/api/stats")
async def get_processing_stats():
    """
    Ackumulerad prestandastatistik över alla lyckade bearbetningar
    (se modules/stats.py): totalt antal, total predikantid, total
    bearbetningstid och kvoten mellan dem (processing_ratio). Används av
    frontend för att uppskatta bearbetningstid för nästa predikan.
    """
    return stats.get_stats(config.STATS_FILE)


@app.get("/api/audio/{file_id}")
async def get_audio_for_playback(file_id: str):
    """Serverar originalfilen så att Wavesurfer.js kan spela upp/rita vågformen."""
    path = UPLOADED_FILES.get(file_id)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Filen hittades inte.")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# STEG 2-6: Klippning -> Metadata -> Transkribering -> AI -> Spreaker -> E-post
# Körs som ett bakgrundsjobb så att frontend kan polla verklig, live status
# inklusive procentuell framdrift per steg.
# ---------------------------------------------------------------------------
class ProcessRequest(BaseModel):
    file_id: str
    start_seconds: float
    end_seconds: float
    speaker: str
    title: str = ""
    description: str = ""
    category: str = ""
    # Valfritt: "YYYY-MM-DDTHH:MM" (från ett <input type="datetime-local">).
    # Tomt = publicera direkt.
    publish_date: str = ""


@app.post("/api/process")
async def start_processing(req: ProcessRequest, background_tasks: BackgroundTasks):
    original_path = UPLOADED_FILES.get(req.file_id)
    if not original_path or not original_path.exists():
        raise HTTPException(status_code=404, detail="Originalfilen hittades inte. Ladda upp igen.")

    if not req.speaker.strip():
        raise HTTPException(status_code=400, detail="Talare är ett obligatoriskt fält.")

    job_id = str(uuid.uuid4())
    JOBS[job_id] = _new_job()

    background_tasks.add_task(_run_processing_job, job_id, req, original_path)

    return {"job_id": job_id}


@app.get("/api/process/status/{job_id}")
async def get_processing_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Jobbet hittades inte.")
    return JSONResponse(job)

def _sanitize_for_filename(s: str) -> str:
    """Ta bort/ersätt ogiltiga tecken för filnamn."""
    s = s.strip()
    # Ersätt mellanslag med bindestreck
    s = re.sub(r"\s+", "-", s)
    # Behåll bara alfanumeriska, bindestreck, underscore och punkt
    s = re.sub(r"[^A-Za-z0-9\-\._]", "", s)
    return s or "unknown"

def _format_publish_date_for_filename(publish_date: str) -> str:
    """
    Förväntar publish_date i formatet 'YYYY-MM-DDTHH:MM' eller tom sträng.
    Returnerar 'YYYYMMDD' eller 'nopub' om tomt/ogiltigt.
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
            return "nopub"

def _run_processing_job(job_id: str, req: ProcessRequest, original_path: Path) -> None:
    """
    Kör själva bearbetningspipelinen. Detta körs i en bakgrundstråd.
    Denna version flyttar originalfilen till uploads/ med ett beskrivande namn
    och sparar alla genererade filer i processed/ med samma basnamn.
    """
    job = JOBS[job_id]
    job_start_time = time.time()
    clip_duration = max(1.0, req.end_seconds - req.start_seconds)
    job["estimated_seconds"] = stats.estimate_processing_seconds(config.STATS_FILE, clip_duration)

    # ---- Bygg ett base-filenamn ----
    orig_stem = original_path.stem
    ext = original_path.suffix.lower()

    speaker_safe = _sanitize_for_filename(req.speaker)
    pubdate_part = _format_publish_date_for_filename(req.publish_date)
    timestamp = datetime.now().strftime("%Y%m%d%H%M")
    orig_safe = _sanitize_for_filename(orig_stem)

    base_name = f"{orig_safe}-{speaker_safe}-{pubdate_part}-{timestamp}"

    # ---- Flytta/byt namn på originalfilen i uploads/ ----
    try:
        new_upload_path = config.UPLOAD_DIR / f"{base_name}{ext}"
        # Flytta filen (behåll original om move misslyckas)
        shutil.move(str(original_path), str(new_upload_path))
        original_path = new_upload_path
        # Uppdatera UPLOADED_FILES mapping så frontend kan fortsätta spela filen
        for fid, p in list(UPLOADED_FILES.items()):
            # jämför Path lika
            if p == original_path or p.samefile(new_upload_path):
                UPLOADED_FILES[fid] = new_upload_path
                break
    except Exception:
        # Om något går fel med rename, fortsätt ändå med original_path
        pass

    # --- STEG 2: Klipp & normalisera ---
    _set_step(job, "trim", "running", percent=0)
    stop_event = threading.Event()
    ticker = threading.Thread(
        target=_run_ticking_estimate,
        args=(job, "trim", max(2.0, clip_duration * 0.05), stop_event),
        daemon=True,
    )
    ticker.start()
    try:
        # Sätt filnamn för output i processed/ med base_name
        clipped_path = config.PROCESSED_DIR / f"{base_name}-clipped.mp3"
        audio_processor.trim_and_normalize(
            original_path, clipped_path, req.start_seconds, req.end_seconds
        )
    except Exception as exc:
        stop_event.set()
        _fail_job(job, "trim", f"Klippning misslyckades: {exc}")
        return
    finally:
        stop_event.set()
    _set_step(job, "trim", "done")

    # --- STEG 4a: Transkribering ---
    _set_step(job, "transcription", "running", percent=0)
    transcription_factor = config.WHISPER_TIME_FACTOR or (1.8 if config.USE_LOCAL_WHISPER else 0.2)
    stop_event = threading.Event()
    ticker = threading.Thread(
        target=_run_ticking_estimate,
        args=(job, "transcription", clip_duration * transcription_factor, stop_event),
        daemon=True,
    )
    ticker.start()
    try:
        transcript = transcription.transcribe_audio(clipped_path)
        # Spara transkriptionen till en textfil i processed/ med base_name
        transcript_path = config.PROCESSED_DIR / f"{base_name}-transcript.txt"
        transcription.save_transcript(transcript, transcript_path)
    except Exception as exc:
        stop_event.set()
        _fail_job(job, "transcription", f"Transkribering misslyckades: {exc}")
        return
    finally:
        stop_event.set()
    _set_step(job, "transcription", "done")

    # --- STEG 4b: AI-berikning (tre separata anrop: titel, beskrivning, taggar) ---
    # Titel/beskrivning genereras bara om användaren lämnat fältet tomt -
    # mindre AI-belastning och bättre kontroll. Taggar genereras alltid,
    # eftersom formuläret inte har något manuellt tagg-alternativ.
    need_title = not req.title.strip()
    need_description = not req.description.strip()
    final_title = req.title.strip()
    final_description = req.description.strip()
    tags: list[str] = []

    _set_step(job, "ai_enrichment", "running", percent=0)
    estimated_per_call = 60.0 if config.AI_PROVIDER == "ollama" else 8.0
    calls_planned = 1 + int(need_title) + int(need_description)  # taggar körs alltid
    stop_event = threading.Event()
    ticker = threading.Thread(
        target=_run_ticking_estimate,
        args=(job, "ai_enrichment", estimated_per_call * calls_planned, stop_event),
        daemon=True,
    )
    ticker.start()
    try:
        if need_title:
            final_title = ai_enrichment.generate_title(transcript, req.speaker, base_name=base_name)
        if need_description:
            final_description = ai_enrichment.generate_description(transcript, req.speaker, base_name=base_name)
        tags = ai_enrichment.generate_tags(transcript, base_name=base_name)
    except Exception as exc:
        stop_event.set()
        _fail_job(job, "ai_enrichment", f"AI-berikning misslyckades: {exc}")
        return
    finally:
        stop_event.set()
    _set_step(job, "ai_enrichment", "done")

    if not final_title:
        final_title = f"Predikan av {req.speaker}"

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

    if not final_title:
        final_title = f"Predikan av {req.speaker}"

    # --- STEG 5: Publicera på Spreaker (verklig uppladdningsprocent) ---
    _set_step(job, "spreaker_publish", "running", percent=0)

    def _on_upload_progress(percent: int) -> None:
        _set_step_percent(job, "spreaker_publish", percent)

    try:
        publish_result = spreaker_client.publish_episode(
            audio_path=clipped_path,
            title=final_title,
            description=final_description,
            tags=tags,
            publish_date=req.publish_date,
            progress_callback=_on_upload_progress,
        )
    except Exception as exc:
        _fail_job(job, "spreaker_publish", f"Publicering på Spreaker misslyckades: {exc}")
        return
    _set_step(job, "spreaker_publish", "done")

    episode_url = publish_result["episode_url"]

    # --- STEG 6: Bekräftelse via e-post (eller sammanfattningssida i UI) ---
    _set_step(job, "email", "running", percent=0)
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
        _set_step(job, "email", "done" if email_sent else "skipped", percent=100)
    except Exception:
        _set_step(job, "email", "error")

    # --- Statistik & lagringsstädning ---
    # Räknas bara för lyckade bearbetningar (annars snedvrids snittet av
    # avbrutna/misslyckade körningar).
    processing_seconds = time.time() - job_start_time
    stats.record_job(config.STATS_FILE, processing_seconds, clip_duration)

    removed_bases = storage_cleanup.enforce_retention(
        config.UPLOAD_DIR, config.PROCESSED_DIR, config.MAX_STORED_EPISODES
    )
    if removed_bases:
        for fid, p in list(UPLOADED_FILES.items()):
            if p.stem in removed_bases:
                UPLOADED_FILES.pop(fid, None)

    job["status"] = "done"
    job["result"] = {
        "final_title": final_title,
        "final_description": final_description,
        "tags": tags,
        "episode_url": episode_url,
        "simulated": publish_result.get("simulated", False),
        "scheduled": publish_result.get("scheduled", False),
        "backdated": publish_result.get("backdated", False),
        "publish_date": req.publish_date,
        "email_sent": email_sent,
        "speaker": req.speaker,
        "category": req.category,
        "processing_seconds": processing_seconds,
        "files": {
            "audio_mp3": str(clipped_path),
            "transcript_txt": str(transcript_path),
            "enrichment_json": str(json_path),
        }
    }


def _fail_job(job: dict, step_key: str, message: str) -> None:
    _set_step(job, step_key, "error")
    job["status"] = "error"
    job["error"] = message


# ---------------------------------------------------------------------------
# CSV-bulkimport: processa flera predikningar i en batch utifrån en CSV-fil
# som pekar på ljudfiler som redan ligger i config.BULK_IMPORT_DIR. Filerna
# klipps inte manuellt - hela filen bearbetas, precis som om start/slut
# vore satt till hela ljudlängden. Batchen körs sekventiellt (en predikan i
# taget) i en egen bakgrundstråd, för att inte överbelasta lokal
# Whisper/Ollama med flera samtidiga tunga jobb.
# ---------------------------------------------------------------------------
BULK_COLUMN_ALIASES = {
    "filename": {"filename", "filnamn", "fil"},
    "speaker": {"speaker", "talare"},
    "date": {"date", "datum"},
    "time": {"time", "klockslag", "tid"},
    "title": {"title", "titel"},
}
BULK_REQUIRED_COLUMNS = {"filename", "speaker", "date", "time"}


def _normalize_csv_headers(fieldnames: list[str] | None) -> dict[str, str]:
    """Mappar faktiska CSV-kolumnnamn (case-insensitive, svenska/engelska) till kanoniska nycklar."""
    mapping: dict[str, str] = {}
    for raw_name in fieldnames or []:
        key = raw_name.strip().lower()
        for canonical, aliases in BULK_COLUMN_ALIASES.items():
            if key in aliases:
                mapping[raw_name] = canonical
                break
    return mapping


def _parse_bulk_csv(raw_text: str) -> list[dict]:
    """
    Läser och validerar CSV-innehållet. Kastar HTTPException(400) med en
    samlad, läsbar felbeskrivning om något är fel - hela batchen valideras
    innan något börjar bearbetas, så ett skrivfel i rad 8 inte upptäcks
    först efter att rad 1-7 redan bearbetats.
    """
    reader = csv.DictReader(io.StringIO(raw_text))
    header_map = _normalize_csv_headers(reader.fieldnames)
    missing_columns = BULK_REQUIRED_COLUMNS - set(header_map.values())
    if missing_columns:
        raise HTTPException(
            status_code=400,
            detail=(
                f"CSV-filen saknar kolumn(er): {', '.join(sorted(missing_columns))}. "
                "Förväntade kolumner: filnamn (filename/filnamn), talare "
                "(speaker/talare), datum (date/datum), klockslag "
                "(time/klockslag/tid), samt valfritt titel (title/titel)."
            ),
        )

    items: list[dict] = []
    errors: list[str] = []

    for row_num, raw_row in enumerate(reader, start=2):  # rad 1 = header
        row = {
            canonical: (raw_row.get(raw_name) or "").strip()
            for raw_name, canonical in header_map.items()
        }
        filename = row.get("filename", "")
        speaker = row.get("speaker", "")
        date_str = row.get("date", "")
        time_str = row.get("time", "")
        title = row.get("title", "")

        row_errors: list[str] = []

        if not filename:
            row_errors.append("filnamn saknas")
        elif Path(filename).suffix.lower() not in config.ALLOWED_EXTENSIONS:
            row_errors.append(f"filtyp stöds ej ('{Path(filename).suffix}')")
        elif not (config.BULK_IMPORT_DIR / filename).exists():
            row_errors.append(f"filen hittades inte i {config.BULK_IMPORT_DIR.name}/")

        if not speaker:
            row_errors.append("talare saknas")

        publish_date = ""
        if not date_str or not time_str:
            row_errors.append("datum eller klockslag saknas")
        else:
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                publish_date = dt.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                row_errors.append(
                    f"ogiltigt datum/klockslag ('{date_str} {time_str}', "
                    "förväntat format ÅÅÅÅ-MM-DD och TT:MM)"
                )

        if row_errors:
            errors.append(f"Rad {row_num} ({filename or '?'}): {'; '.join(row_errors)}")
            continue

        items.append({
            "filename": filename,
            "speaker": speaker,
            "title": title,
            "publish_date": publish_date,
        })

    if errors:
        raise HTTPException(status_code=400, detail="Fel i CSV-filen:\n" + "\n".join(errors))
    if not items:
        raise HTTPException(status_code=400, detail="CSV-filen innehöll inga datarader att importera.")

    return items


@app.post("/api/bulk-import")
async def bulk_import(file: UploadFile = File(...)):
    raw_bytes = await file.read()
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV-filen måste vara UTF-8-kodad.")

    items = _parse_bulk_csv(raw_text)

    batch_items = []
    for item in items:
        job_id = str(uuid.uuid4())
        JOBS[job_id] = _new_job()
        batch_items.append({**item, "job_id": job_id})

    batch_id = str(uuid.uuid4())
    BATCHES[batch_id] = {"items": batch_items, "status": "running"}

    thread = threading.Thread(target=_run_bulk_batch, args=(batch_id,), daemon=True)
    thread.start()

    return {
        "batch_id": batch_id,
        "items": [
            {"job_id": it["job_id"], "filename": it["filename"], "speaker": it["speaker"]}
            for it in batch_items
        ],
    }


def _run_bulk_batch(batch_id: str) -> None:
    batch = BATCHES[batch_id]
    for item in batch["items"]:
        job = JOBS[item["job_id"]]
        original_path = config.BULK_IMPORT_DIR / item["filename"]
        try:
            duration = audio_processor.get_audio_duration_seconds(original_path)
        except Exception as exc:
            _fail_job(job, "trim", f"Kunde inte läsa ljudfilen: {exc}")
            continue

        req = ProcessRequest(
            file_id="bulk-import",
            start_seconds=0,
            end_seconds=duration,
            speaker=item["speaker"],
            title=item["title"],
            publish_date=item["publish_date"],
        )
        _run_processing_job(item["job_id"], req, original_path)

    batch["status"] = "done"


@app.get("/api/bulk-import/status/{batch_id}")
async def get_bulk_import_status(batch_id: str):
    batch = BATCHES.get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batchen hittades inte.")

    items = []
    for it in batch["items"]:
        job = JOBS.get(it["job_id"], {})
        items.append({
            "job_id": it["job_id"],
            "filename": it["filename"],
            "speaker": it["speaker"],
            "status": job.get("status"),
            "overall_percent": job.get("overall_percent", 0),
            "error": job.get("error"),
            "result": job.get("result"),
        })

    return {"batch_id": batch_id, "status": batch["status"], "items": items}


# ---------------------------------------------------------------------------
# Frontend (statiska filer)
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory="static", html=True), name="static")
