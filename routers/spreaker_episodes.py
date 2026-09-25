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

Undantaget är podd-arkivet (/archive/...), som bara läser showens PUBLIKA
RSS-flöde och därför bara kräver SPREAKER_SHOW_ID (se modules/podcast_archive.py).
"""
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config
from modules import podcast_archive, queue_store, spreaker_client, spreaker_episode_store
from modules.spreaker_client import SpreakerUploadError

router = APIRouter(prefix="/api/spreaker", tags=["spreaker-episodes"])


class EpisodeEdit(BaseModel):
    """
    Nya värden när en rad sparas i Hantera Spreaker (PUT /episodes/{id}).
    """
    title: str
    description: str = ""


class RegenerateRequest(BaseModel):
    """
    Vad "Generera om" ska göra för ett avsnitt.

    regenerate_title/regenerate_description: vilka fält som ska få ett nytt
    AI-förslag. force_retranscribe: transkribera om även om ett sparat
    transkript finns (kryssrutan "Transkribera om").
    """
    regenerate_title: bool = False
    regenerate_description: bool = False
    force_retranscribe: bool = False


def _is_configured() -> bool:
    """
    Om avsnittshanteringen kan användas: token och show-id finns och simulering är av.

    Returns:
        True om riktiga anrop mot Spreaker är tillåtna.
    """
    return bool(config.SPREAKER_API_TOKEN) and bool(config.SPREAKER_SHOW_ID) and not config.SPREAKER_SIMULATE


def _require_configured() -> None:
    """
    Stoppar anropet med 403 om avsnittshanteringen inte är konfigurerad.

    Anropas först i varje endpoint - att fliken är dold i webbläsaren räcker
    inte som skydd, eftersom endpointsen kan anropas direkt.
    """
    if not _is_configured():
        raise HTTPException(
            status_code=403,
            detail="Spreaker är inte konfigurerat för hantering (kräver SPREAKER_API_TOKEN, "
            "SPREAKER_SHOW_ID och SPREAKER_SIMULATE=false).",
        )


@router.get("/status")
async def get_status():
    """
    GET /api/spreaker/status - vad Spreaker-fliken ska visa.

    Returns:
        {"configured": avsnittslistan kan användas,
         "archive_available": podd-arkivet kan användas}.
    """
    return {"configured": _is_configured(), "archive_available": podcast_archive.is_available()}


def _require_archive_available() -> None:
    """
    Stoppar anropet med 403 om SPREAKER_SHOW_ID saknas (arkivet behöver det).
    """
    if not podcast_archive.is_available():
        raise HTTPException(status_code=403, detail="Podd-arkivet kräver SPREAKER_SHOW_ID.")


@router.get("/archive/status")
async def get_archive_status():
    """
    GET /api/spreaker/archive/status - hur arkiveringen går.

    Returns:
        Statusen från podcast_archive.get_status() (pågår, avsnitt x av y,
        nedladdat, misslyckade ...).
    """
    _require_archive_available()
    return podcast_archive.get_status()


@router.post("/archive/run")
async def run_archive():
    """
    POST /api/spreaker/archive/run - startar en arkivering i bakgrunden.

    Returns:
        Statusen direkt efter start.

    Raises:
        HTTPException 409: Om en arkivering redan pågår.
    """
    _require_archive_available()
    if not podcast_archive.start():
        raise HTTPException(status_code=409, detail="Arkiveringen pågår redan.")
    return podcast_archive.get_status()


@router.post("/archive/stop")
async def stop_archive():
    """
    POST /api/spreaker/archive/stop - ber en pågående arkivering att avbryta.

    Returns:
        Statusen (med stopping=true tills körningen hunnit stanna).
    """
    _require_archive_available()
    podcast_archive.stop()
    return podcast_archive.get_status()


def _with_archive_info(items: list[dict]) -> list[dict]:
    """
    Kompletterar avsnittslistan med vad som finns i det lokala podd-arkivet:
    "archived" (mp3:an finns lokalt, så "Generera om" slipper ladda ner) och
    has_transcript även när transkriptet bara finns som fil i arkivet.

    Args:
        items: Avsnitten från den lokala listan.

    Returns:
        Samma lista, där varje avsnitt fått fälten archived och
        has_transcript (ändras på plats och returneras för bekvämlighet).
    """
    idx = podcast_archive.index()
    for item in items:
        local = podcast_archive.local_info(item["episode_id"], idx)
        item["archived"] = local["archived"]
        item["has_transcript"] = bool(item.get("has_transcript")) or local["has_transcript"]
    return items


@router.get("/episodes")
async def get_cached_episodes():
    """
    GET /api/spreaker/episodes - den lokalt sparade avsnittslistan.

    Gör inga anrop mot Spreaker, så fliken öppnas direkt.

    Returns:
        {"items": [...]} med ett avsnitt per rad, inklusive arkivinfo.
    """
    _require_configured()
    return {"items": _with_archive_info(spreaker_episode_store.get_all())}


@router.post("/episodes/fetch")
async def fetch_episodes():
    """
    POST /api/spreaker/episodes/fetch - hämtar en färsk lista från Spreaker.

    Klicket på "🔄 Hämta från Spreaker". Ersätter hela den lokala listan.

    Returns:
        {"items": [...]} - den nya listan.

    Raises:
        HTTPException 502: Om Spreaker svarar med ett fel.
    """
    _require_configured()
    try:
        raw_episodes = spreaker_client.list_episodes()
    except SpreakerUploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    spreaker_episode_store.replace_all(raw_episodes)
    return {"items": _with_archive_info(spreaker_episode_store.get_all())}


@router.put("/episodes/{episode_id}")
async def update_episode(episode_id: int, edit: EpisodeEdit):
    """
    PUT /api/spreaker/episodes/{episode_id} - sparar titel och beskrivning till Spreaker.

    Används av både "💾 Spara" på en rad och "Spara ändringar" för alla.
    Ändringen skickas till Spreaker FÖRST - bara om den lyckas uppdateras
    den lokala listan, så att listan aldrig visar något som inte sparats.

    Args:
        episode_id: Spreakers id för avsnittet.
        edit: Den nya titeln och beskrivningen.

    Returns:
        {"updated": true}.

    Raises:
        HTTPException 400: Om titeln är tom.
        HTTPException 502: Om Spreaker avvisar ändringen.
    """
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
