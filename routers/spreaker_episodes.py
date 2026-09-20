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
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config
from modules import spreaker_client, spreaker_episode_store
from modules.spreaker_client import SpreakerUploadError

router = APIRouter(prefix="/api/spreaker", tags=["spreaker-episodes"])


class EpisodeEdit(BaseModel):
    title: str
    description: str = ""


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
