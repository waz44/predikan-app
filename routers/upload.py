"""
Router: upload
STEG 1: Uppladdning av originalfilen (innan den ev. läggs i bearbetningskön)
samt uppspelning av den för vågformen (Wavesurfer.js) i frontend.
"""
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

import config
from modules import audio_processor
from services import state

router = APIRouter(prefix="/api", tags=["upload"])

_UPLOAD_CHUNK = 1024 * 1024  # 1 MB


def _too_large_detail() -> str:
    """
    Felmeddelandet när en uppladdad fil är större än MAX_UPLOAD_MB.

    Returns:
        Text som visas för användaren, med den aktuella gränsen i MB.
    """
    return f"Filen är för stor. Största tillåtna uppladdning är {config.MAX_UPLOAD_MB} MB."


@router.post("/upload")
async def upload_audio(file: UploadFile = File(...)):
    """
    POST /api/upload - tar emot en ljudfil från formulärets filväljare.

    Filen sparas i uploads/ under ett slumpat namn och får ett id, som
    sedan används för att spela upp den i vågformen och för att köa den.
    Filen strömmas till disk i bitar med löpande storlekskontroll, så att
    en jättefil aldrig hinner fylla disken innan den avvisas.

    Args:
        file: Den uppladdade filen.

    Returns:
        {"file_id", "filename", "duration_seconds"} - längden används för
        att ställa in klippningens slutpunkt.

    Raises:
        HTTPException 400: Otillåten filtyp eller oläslig ljudfil.
        HTTPException 413: Filen är större än MAX_UPLOAD_MB.
    """
    ext = Path(file.filename).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Filtyp '{ext}' stöds ej. Tillåtna format: {config.ALLOWED_EXTENSIONS}",
        )

    file_id = str(uuid.uuid4())
    dest_path = config.UPLOAD_DIR / f"{file_id}{ext}"

    # Strömma till disk i bitar med en löpande storlekskoll, i stället för
    # shutil.copyfileobj rakt av, så en fil större än gränsen avbryts direkt
    # (och den halvskrivna filen städas bort) i stället för att först skrivas
    # färdigt och fylla disken. 0 = ingen gräns (se config.MAX_UPLOAD_BYTES).
    limit = config.MAX_UPLOAD_BYTES
    written = 0
    try:
        with dest_path.open("wb") as f:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if limit and written > limit:
                    raise HTTPException(status_code=413, detail=_too_large_detail())
                f.write(chunk)
    except HTTPException:
        dest_path.unlink(missing_ok=True)
        raise

    state.UPLOADED_FILES[file_id] = dest_path

    try:
        duration = audio_processor.get_audio_duration_seconds(dest_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Kunde inte läsa ljudfilen: {exc}") from exc

    return {"file_id": file_id, "filename": file.filename, "duration_seconds": duration}


@router.get("/audio/{file_id}")
async def get_audio_for_playback(file_id: str):
    """
    Serverar originalfilen så att Wavesurfer.js kan spela upp/rita vågformen.

    Args:
        file_id: Id från svaret på POST /api/upload.

    Returns:
        Själva ljudfilen, som vågformen i webbläsaren laddar och spelar upp.

    Raises:
        HTTPException 404: Om id:t är okänt eller filen har tagits bort.
    """
    path = state.UPLOADED_FILES.get(file_id)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Filen hittades inte.")
    return FileResponse(path)


@router.post("/audio/{file_id}/normalize")
def normalize_uploaded_audio(file_id: str):
    """
    POST /api/audio/{file_id}/normalize - normaliserar ljudnivån i en
    uppladdad fil innan den klipps (knappen "Normalisera ljud" i steg 2).

    Körs DIREKT i anropet och går helt förbi bearbetningskön - användaren
    väntar på resultatet för att kunna lyssna och välja klippunkter i det
    normaliserade ljudet. (Vanlig def, inte async: FastAPI kör den då i en
    egen tråd, så att servern inte står still medan ffmpeg arbetar.)

    Den normaliserade filen ersätter originalet under samma file_id, så att
    både vågformen och en senare köläggning använder den. Originalet tas
    bort för att inte bli liggande i uploads/.

    Args:
        file_id: Id från svaret på POST /api/upload.

    Returns:
        {"file_id", "duration_seconds"} - längden kan skilja några
        millisekunder efter omkodningen till mp3.

    Raises:
        HTTPException 404: Om id:t är okänt eller filen har tagits bort.
        HTTPException 500: Om normaliseringen misslyckas.
    """
    path = state.UPLOADED_FILES.get(file_id)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Filen hittades inte. Ladda upp igen.")

    # Nytt namn även om originalet redan är mp3 - ffmpeg kan inte läsa och
    # skriva samma fil samtidigt.
    dest_path = config.UPLOAD_DIR / f"{file_id}-normalized-{uuid.uuid4().hex[:8]}.mp3"
    try:
        audio_processor.normalize_loudness(path, dest_path)
        duration = audio_processor.get_audio_duration_seconds(dest_path)
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Normalisering misslyckades: {exc}") from exc

    state.UPLOADED_FILES[file_id] = dest_path
    path.unlink(missing_ok=True)

    return {"file_id": file_id, "duration_seconds": duration}
