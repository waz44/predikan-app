"""
Router: spreaker_episodes
"Hantera Spreaker"-fliken i frontend: visa/redigera avsnitt som redan
ligger uppe på det RIKTIGA Spreaker-kontot, via en lokal cache (se
modules/spreaker_episode_store.py) för snabb visning/sortering.

Till skillnad från routers/process.py (som lyder config.SPREAKER_SIMULATE)
gör den här routern ALLTID riktiga API-anrop när den överhuvudtaget är
tillgänglig - se _require_configured, som stänger av alla tre endpoints
(inte bara döljer fliken i frontend) om token/show-id saknas ELLER
SIMULATE är på. Det finns inget meningsfullt "simulerat" läge för att
redigera redan publicerat innehåll.
"""
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config
from modules import queue_store, spreaker_client, spreaker_episode_store
from modules.spreaker_client import SpreakerUploadError

router = APIRouter(prefix="/api/spreaker", tags=["spreaker-episodes"])


class EpisodeEdit(BaseModel):
    title: str
    description: str = ""


class RegenerateRequest(BaseModel):
    regenerate_title: bool = False
    regenerate_description: bool = False
    force_retranscribe: bool = False


def _is_configured() -> bool:
    return bool(config.SPREAKER_API_TOKEN) and bool(config.SPREAKER_SHOW_ID) and not config.SPREAKER_SIMULATE


def _require_configured() -> None:
    if not _is_configured():
        raise HTTPException(
            status_code=403,
            detail="Spreaker är inte konfigurerat för hantering (kräver SPREAKER_API_TOKEN, "
            "SPREAKER_SHOW_ID och SPREAKER_SIMULATE=false).",
        )


@router.get("/status")
async def get_status():
    return {"configured": _is_configured()}


@router.get("/episodes")
async def get_cached_episodes():
    _require_configured()
    return {"items": spreaker_episode_store.get_all()}


@router.post("/episodes/fetch")
async def fetch_episodes():
    _require_configured()
    try:
        raw_episodes = spreaker_client.list_episodes()
    except SpreakerUploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    spreaker_episode_store.replace_all(raw_episodes)
    return {"items": spreaker_episode_store.get_all()}


@router.put("/episodes/{episode_id}")
async def update_episode(episode_id: int, edit: EpisodeEdit):
    _require_configured()
    if not edit.title.strip():
        raise HTTPException(status_code=400, detail="Titel är obligatoriskt.")
    try:
        spreaker_client.update_episode(episode_id, edit.title, edit.description)
    except SpreakerUploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    spreaker_episode_store.update_local(episode_id, edit.title, edit.description)
    return {"updated": True}


@router.post("/episodes/{episode_id}/regenerate")
async def regenerate_episode(episode_id: int, req: RegenerateRequest):
    """
    Lägger ett "Generera om"-jobb i den vanliga bearbetningskön (se
    services/pipeline.py:_run_regenerate_job) - laddar ner avsnittets
    ljud (om inget transkript redan är cachat sen tidigare) och genererar
    ett NYTT AI-förslag på titel och/eller beskrivning. Skriver ALDRIG
    till Spreaker direkt - resultatet hämtas via GET /api/process/status/{job_id}
    och fylls i redigeringsfälten i frontend, som sedan sparas på vanligt
    sätt (PUT ovan) om användaren väljer att behålla det.
    """
    _require_configured()
    if not req.regenerate_title and not req.regenerate_description:
        raise HTTPException(status_code=400, detail="Välj minst titel eller beskrivning att generera om.")

    cached = spreaker_episode_store.get(episode_id)
    filename = (cached or {}).get("title") or f"Avsnitt {episode_id}"
    speaker = (cached or {}).get("speaker") or ""

    job_id = str(uuid.uuid4())
    queue_id = str(uuid.uuid4())
    fields = {
        "episode_id": episode_id,
        "regenerate_title": req.regenerate_title,
        "regenerate_description": req.regenerate_description,
        "force_retranscribe": req.force_retranscribe,
    }
    # original_path/keep_original är inte meningsfulla för den här kinden -
    # _run_regenerate_job hämtar ljudet direkt via episode_id istället.
    queue_store.add(
        queue_id, job_id, "regenerate", filename, speaker, fields,
        Path(f"spreaker-episode-{episode_id}"), False, datetime.now().isoformat(),
    )
    return {"job_id": job_id, "queue_id": queue_id}
