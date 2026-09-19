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
    item = queue_store.get_by_job_id(job_id)
    if not item:
        raise HTTPException(status_code=404, detail="Jobbet hittades inte.")
    return queue_item_view(item)
