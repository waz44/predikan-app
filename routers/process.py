"""
Router: process
STEG 2-6, manuellt flöde: lägger en klippt predikan till i bearbetningskön
(se services/pipeline.py + modules/queue_store.py), samt låter frontend
polla ett enskilt jobbs status.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException

from modules import queue_store
from services import state
from services.pipeline import ProcessRequest, queue_item_view

router = APIRouter(prefix="/api", tags=["process"])


@router.post("/process")
async def start_processing(req: ProcessRequest):
    """
    POST /api/process - lägger en uppladdad och klippt predikan i bearbetningskön.

    Anropas när användaren klickar "➕ Lägg till i kö". Jobbet körs inte här
    utan av kö-arbetartråden (services/pipeline.py), så svaret kommer direkt
    och formuläret kan återställas för nästa predikan.

    Args:
        req: Formulärets värden (fil-id, klipp, talare, titel ...).

    Returns:
        {"job_id": ..., "queue_id": ...} - job_id används för att fråga om
        status, queue_id för att ta bort eller prioritera raden.

    Raises:
        HTTPException 404: Om den uppladdade filen inte hittas (t.ex. efter
            en omstart, då id:t inte längre är känt).
    """
    original_path = state.UPLOADED_FILES.get(req.file_id)
    if not original_path or not original_path.exists():
        raise HTTPException(status_code=404, detail="Originalfilen hittades inte. Ladda upp igen.")

    if not req.speaker.strip():
        raise HTTPException(status_code=400, detail="Talare är ett obligatoriskt fält.")

    job_id = str(uuid.uuid4())
    queue_id = str(uuid.uuid4())
    fields = {
        "start_seconds": req.start_seconds,
        "end_seconds": req.end_seconds,
        "speaker": req.speaker,
        "title": req.title,
        "description": req.description,
        "category": req.category,
        "publish_date": req.publish_date,
    }
    queue_store.add(
        queue_id, job_id, "manual", original_path.name, req.speaker, fields,
        original_path, False, datetime.now().isoformat(),
    )

    return {"job_id": job_id, "queue_id": queue_id}


@router.get("/process/status/{job_id}")
async def get_processing_status(job_id: str):
    """
    GET /api/process/status/{job_id} - status för ett jobb.

    Används av "Generera om" i Hantera Spreaker, som pollar sitt eget jobb
    tills förslaget är klart.

    Args:
        job_id: Jobbets id (från svaret på POST /api/process eller
            /api/spreaker/episodes/{id}/regenerate).

    Returns:
        Samma form som en rad i GET /api/queue (se pipeline.queue_item_view).

    Raises:
        HTTPException 404: Om jobbet inte finns (t.ex. borttaget ur kön).
    """
    item = queue_store.get_by_job_id(job_id)
    if not item:
        raise HTTPException(status_code=404, detail="Jobbet hittades inte.")
    return queue_item_view(item)
