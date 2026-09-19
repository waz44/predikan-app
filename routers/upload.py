"""
Router: upload
STEG 1: Uppladdning av originalfilen (innan den ev. läggs i bearbetningskön)
samt uppspelning av den för vågformen (Wavesurfer.js) i frontend.
"""
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

import config
from modules import audio_processor
from services import state

router = APIRouter(prefix="/api", tags=["upload"])


@router.post("/upload")
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

    state.UPLOADED_FILES[file_id] = dest_path

    try:
        duration = audio_processor.get_audio_duration_seconds(dest_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Kunde inte läsa ljudfilen: {exc}") from exc

    return {"file_id": file_id, "filename": file.filename, "duration_seconds": duration}


@router.get("/audio/{file_id}")
async def get_audio_for_playback(file_id: str):
    """Serverar originalfilen så att Wavesurfer.js kan spela upp/rita vågformen."""
    path = state.UPLOADED_FILES.get(file_id)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Filen hittades inte.")
    return FileResponse(path)
