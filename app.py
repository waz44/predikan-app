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
from pathlib import Path

import re
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from modules import audio_processor, transcription, ai_enrichment, spreaker_client, email_notifier

app = FastAPI(title="Predikan → Podcast")

# Håller reda på uppladdade original-filer under körningens gång (file_id -> path)
UPLOADED_FILES: dict[str, Path] = {}

# Håller reda på pågående/klara bearbetningsjobb (job_id -> status-dict)
JOBS: dict[str, dict] = {}

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
    clip_duration = max(1.0, req.end_seconds - req.start_seconds)

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

    # --- STEG 4b: AI-berikning (endast för tomma fält) ---
    need_title = not req.title.strip()
    need_description = not req.description.strip()
    tags: list[str] = []
    final_title = req.title.strip()
    final_description = req.description.strip()
    enrichment_result = {}

    if need_title or need_description:
        _set_step(job, "ai_enrichment", "running", percent=0)
        estimated = 60.0 if config.AI_PROVIDER == "ollama" else 8.0
        stop_event = threading.Event()
        ticker = threading.Thread(
            target=_run_ticking_estimate,
            args=(job, "ai_enrichment", estimated, stop_event),
            daemon=True,
        )
        ticker.start()
        try:
            enriched = ai_enrichment.enrich_metadata(
                transcript=transcript,
                speaker=req.speaker,
                need_title=need_title,
                need_description=need_description,
            )
            if need_title:
                final_title = enriched["title"]
            if need_description:
                final_description = enriched["description"]
            tags = enriched.get("tags", [])

            # Spara AI-berikningen till JSON-fil i processed/ med base_name
            enrichment_result = {
                "title": final_title,
                "description": final_description,
                "tags": tags,
                "speaker": req.speaker,
                "quality_flag": enriched.get("quality_flag", False),
            }
            json_path = config.PROCESSED_DIR / f"{base_name}-enrichment.json"
            ai_enrichment.save_enrichment_result(enrichment_result, json_path)
        except Exception as exc:
            stop_event.set()
            _fail_job(job, "ai_enrichment", f"AI-berikning misslyckades: {exc}")
            return
        finally:
            stop_event.set()
        _set_step(job, "ai_enrichment", "done")
    else:
        _set_step(job, "ai_enrichment", "skipped", percent=100)
        enrichment_result = {
            "title": final_title,
            "description": final_description,
            "tags": tags,
            "speaker": req.speaker,
            "quality_flag": False,
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
        )
        _set_step(job, "email", "done" if email_sent else "skipped", percent=100)
    except Exception:
        _set_step(job, "email", "error")

    job["status"] = "done"
    job["result"] = {
        "final_title": final_title,
        "final_description": final_description,
        "tags": tags,
        "episode_url": episode_url,
        "simulated": publish_result.get("simulated", False),
        "scheduled": publish_result.get("scheduled", False),
        "publish_date": req.publish_date,
        "email_sent": email_sent,
        "speaker": req.speaker,
        "category": req.category,
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
# Frontend (statiska filer)
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory="static", html=True), name="static")
