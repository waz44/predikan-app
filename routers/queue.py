"""
Router: queue
Bearbetningskön: se/pausa/starta/avbryta/prioritera/ta bort/rensa. Kön är
gemensam för manuellt klippta predikningar (routers/process.py) och
CSV-bulkimport (routers/bulk_import.py) - se services/pipeline.py för hur
den faktiskt bearbetas, och modules/queue_store.py för hur den lagras.
"""
from fastapi import APIRouter, HTTPException

from modules import app_logging, queue_store
from services import state
from services.pipeline import queue_item_view

router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("")
async def get_queue():
    """
    GET /api/queue - hela bearbetningskön, som visas i kolumnen till höger.

    Sidan hämtar den var 1,5 sekund. Det jobb som körs just nu får med sin
    live-framdrift per steg (se pipeline.queue_item_view).

    Returns:
        {"paused": bool, "current_queue_id": id eller None, "items": [...]}.
    """
    items = [queue_item_view(it) for it in queue_store.get_all()]
    return {
        "paused": queue_store.get_paused(),
        "current_queue_id": state.get_current_queue_id(),
        "items": items,
    }


@router.post("/pause")
async def pause_queue():
    """
    Pausar kön: inget NYTT objekt plockas upp härefter. Ett objekt som
    redan påbörjats fortsätter köras tills du antingen avbryter det
    (POST /api/queue/cancel/{job_id}) eller det blir klart av sig självt.

    Returns:
        {"paused": true}.
    """
    queue_store.set_paused(True)
    app_logging.logger.info("Bearbetningskön pausad.")
    return {"paused": True}


@router.post("/resume")
async def resume_queue():
    """
    POST /api/queue/resume - startar kön igen ("▶ Starta").

    Nästa väntande objekt plockas upp inom en halv sekund.

    Returns:
        {"paused": false}.
    """
    queue_store.set_paused(False)
    app_logging.logger.info("Bearbetningskön återupptagen.")
    return {"paused": False}


@router.post("/cancel/{job_id}")
async def cancel_queue_item(job_id: str):
    """
    Avbryter ett pågående jobb. Sitter jobbet just då i transkriberings-
    steget dödas den separata transkriberingsprocessen på riktigt (se
    modules/transcription_worker.py) - CPU/GPU frigörs direkt. I övriga
    steg (klippning, AI-berikning) avbryts jobbet så snart det pågående
    steget är klart, eftersom de inte kan avbrytas mitt i på samma säkra
    sätt. Efter att avsnittet publicerats på Spreaker går det inte längre
    att avbryta (kan inte ångras).

    Args:
        job_id: Jobbets id.

    Returns:
        {"cancelled": true} om avbrottet begärdes.

    Raises:
        HTTPException 404: Om jobbet inte finns.
        HTTPException 409: Om jobbet redan är klart.
    """
    item = queue_store.get_by_job_id(job_id)
    if not item:
        raise HTTPException(status_code=404, detail="Jobbet hittades inte.")
    if item["status"] != "running":
        return {"cancelled": False, "reason": "Jobbet körs inte just nu."}

    cancel_event = state.CANCEL_EVENTS.get(job_id)
    if not cancel_event:
        return {"cancelled": False, "reason": "Jobbet kan inte avbrytas just nu."}

    cancel_event.set()
    app_logging.logger.info(f"Avbrytning begärd för jobb {job_id}.")
    return {"cancelled": True}


@router.delete("/{queue_id}")
async def remove_queue_item(queue_id: str):
    """
    Tar bort en enskild rad ur kön (väntande, klar, misslyckad eller avbruten). Ett pågående jobb måste avbrytas först.

    Args:
        queue_id: Radens id i kön.

    Returns:
        {"removed": true}.

    Raises:
        HTTPException 404: Om raden inte finns.
        HTTPException 409: Om raden bearbetas just nu (avbryt den först).
    """
    item = queue_store.get(queue_id)
    if not item:
        raise HTTPException(status_code=404, detail="Objektet hittades inte i kön.")
    if item["status"] == "running":
        raise HTTPException(
            status_code=400,
            detail="Objektet bearbetas just nu - avbryt det först (🚫 Avbryt) innan det kan tas bort.",
        )
    queue_store.remove(queue_id)
    app_logging.logger.info(f"Köobjekt {queue_id} borttaget.")
    return {"removed": True}


@router.post("/clear-errors")
async def clear_queue_errors():
    """
    Tar bort alla rader i kön som misslyckats (t.ex. 'filen hittades inte' vid en CSV-omkörning) eller avbrutits.

    Returns:
        {"removed": antal borttagna rader}.
    """
    removed = queue_store.remove_where_status_in(["error", "cancelled"])
    app_logging.logger.info(f"Rensade {removed} misslyckade/avbrutna rad(er) ur kön.")
    return {"removed": removed}


@router.post("/clear-done")
async def clear_queue_done():
    """
    Tar bort alla rader i kön som blivit klara - praktiskt för att hålla kölistan kort efter en stor batch.

    Returns:
        {"removed": antal borttagna rader}.
    """
    removed = queue_store.remove_where_status_in(["done"])
    app_logging.logger.info(f"Rensade {removed} klar(a) rad(er) ur kön.")
    return {"removed": removed}


@router.post("/clear")
async def clear_queue():
    """
    Tömmer hela kön - allt utom det objekt som eventuellt bearbetas just nu (det påverkas inte, avbryt det separat om så önskas).

    Returns:
        {"removed": antal borttagna rader}.
    """
    removed = queue_store.remove_where_status_not_in(["running"])
    app_logging.logger.info(f"Rensade hela kön ({removed} rad(er)).")
    return {"removed": removed}


@router.post("/prioritize/{queue_id}")
async def prioritize_queue_item(queue_id: str):
    """
    Flyttar ett väntande objekt längst fram i kön, så det bearbetas härnäst.

    Args:
        queue_id: Radens id i kön.

    Returns:
        {"prioritized": true}.

    Raises:
        HTTPException 409: Om raden inte väntar (redan körs eller är klar).
    """
    item = queue_store.get(queue_id)
    if not item:
        raise HTTPException(status_code=404, detail="Objektet hittades inte i kön.")
    if not queue_store.move_to_front(queue_id):
        raise HTTPException(status_code=400, detail="Bara objekt som väntar i kön kan prioriteras om.")
    app_logging.logger.info(f"Köobjekt {queue_id} prioriterat till köns början.")
    return {"prioritized": True}
