"""
Router: archive
Det lokala podd-arkivet (⚙️ Inställningar → 🗄️ Podd-arkiv): starta, stoppa
och följa en arkivering, se modules/podcast_archive.py. Arkivet läser
showens PUBLIKA RSS-flöde hos Spreaker och kräver därför bara
SPREAKER_SHOW_ID - ingen token.

Adresserna ligger kvar under /api/spreaker/archive, där de legat sedan
arkivet byggdes.
"""
from fastapi import APIRouter, HTTPException

from modules import archive_scheduler, podcast_archive

router = APIRouter(prefix="/api/spreaker/archive", tags=["archive"])


def _require_archive_available() -> None:
    """
    Stoppar anropet med 403 om SPREAKER_SHOW_ID saknas (arkivet behöver det).
    """
    if not podcast_archive.is_available():
        raise HTTPException(status_code=403, detail="Podd-arkivet kräver SPREAKER_SHOW_ID.")


def _archive_status() -> dict:
    """
    Arkiveringens status plus nästa schemalagda körning.

    Returns:
        podcast_archive.get_status() med fältet next_scheduled_run (ISO-tid,
        eller None när schemat är avstängt).
    """
    status = podcast_archive.get_status()
    next_run = archive_scheduler.next_scheduled_run()
    status["next_scheduled_run"] = next_run.isoformat(timespec="minutes") if next_run else None
    return status


@router.get("/status")
async def get_archive_status():
    """
    GET /api/spreaker/archive/status - hur arkiveringen går.

    Returns:
        Statusen från podcast_archive.get_status() (pågår, avsnitt x av y,
        nedladdat, misslyckade ...) och nästa schemalagda körning.
    """
    _require_archive_available()
    return _archive_status()


@router.post("/run")
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
    return _archive_status()


@router.post("/stop")
async def stop_archive():
    """
    POST /api/spreaker/archive/stop - ber en pågående arkivering att avbryta.

    Returns:
        Statusen (med stopping=true tills körningen hunnit stanna).
    """
    _require_archive_available()
    podcast_archive.stop()
    return _archive_status()
