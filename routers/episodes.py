"""
Router: episodes
Fliken 📡 Avsnitt i frontend: de publicerade avsnitten - tjänstens lista
och appens egen historik, sammanslagna (se modules/episode_library.py).

Allt som rör själva tjänsten går via modules/publishers, och varje
endpoint kontrollerar vad tjänsten klarar (Capabilities) - att en knapp
är dold i webbläsaren räcker inte som skydd, eftersom endpointsen kan
anropas direkt.

Avsnittens id är text: tjänstens id ("75397245") eller, för avsnitt som
bara finns i appens historik, "h" + radens id ("h12").
"""
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from modules import episode_library, episode_store, queue_store
from modules.publishers import PublisherError, get_publisher

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


class EpisodeEdit(BaseModel):
    """
    Nya värden när ett avsnitt sparas (PUT /api/episodes/{id}).
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


@router.get("/status")
async def get_status():
    """
    GET /api/episodes/status - om fliken Avsnitt ska visas, och för vilken tjänst.

    Fliken visas när tjänsten publicerar på riktigt eller när appen har
    publicerat något (även simulerat) - då finns det något att visa.

    Returns:
        {"visible", "provider": {"key", "label", "live", "capabilities"}}.
    """
    publisher = get_publisher()
    return {
        "visible": publisher.is_live() or bool(episode_store.list_published()),
        "provider": {
            "key": publisher.key,
            "label": publisher.label,
            "live": publisher.is_live(),
            "capabilities": publisher.capabilities().as_dict(),
        },
    }


@router.get("")
async def list_episodes():
    """
    GET /api/episodes - den sammanslagna avsnittslistan.

    Gör inga anrop mot tjänsten (bara den lokalt sparade listan och appens
    historik), så fliken öppnas direkt.

    Returns:
        {"provider": {...}, "items": [...]} - se episode_library.list_items.
    """
    return episode_library.list_items()


@router.post("/fetch")
async def fetch_episodes():
    """
    POST /api/episodes/fetch - hämtar en färsk avsnittslista från tjänsten.

    Klicket på "🔄 Hämta från ...". Ersätter den lokalt sparade listan.

    Returns:
        Samma form som GET /api/episodes.

    Raises:
        HTTPException 403: Om tjänsten inte kan lista avsnitt (ej inställd,
            simulerad eller saknar stöd).
        HTTPException 502: Om tjänsten svarar med ett fel.
    """
    publisher = get_publisher()
    if not publisher.capabilities().list_episodes:
        raise HTTPException(status_code=403, detail=f"{publisher.label} kan inte lista avsnitt just nu.")
    try:
        publisher.fetch_episodes()
    except PublisherError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return episode_library.list_items(publisher)


@router.put("/{item_id}")
async def update_episode(item_id: str, edit: EpisodeEdit):
    """
    PUT /api/episodes/{id} - sparar titel och beskrivning hos tjänsten.

    Används av både "💾 Spara" på ett avsnitt och "Spara ändringar" för
    alla. Ändringen skickas till tjänsten först - bara om den lyckas
    uppdateras den lokala listan.

    Args:
        item_id: Tjänstens id för avsnittet.
        edit: Den nya titeln och beskrivningen.

    Returns:
        {"updated": true}.

    Raises:
        HTTPException 400: Om titeln är tom.
        HTTPException 403: Om tjänsten inte kan uppdatera avsnitt, eller om
            avsnittet bara finns i appens historik.
        HTTPException 502: Om tjänsten avvisar ändringen.
    """
    publisher = get_publisher()
    if not publisher.capabilities().update_episode or episode_library.history_id_of(item_id) is not None:
        raise HTTPException(
            status_code=403,
            detail=f"Avsnittet kan inte uppdateras hos {publisher.label} härifrån - kopiera texten i stället.",
        )
    if not edit.title.strip():
        raise HTTPException(status_code=400, detail="Titel är obligatoriskt.")
    try:
        publisher.update_episode(item_id, edit.title, edit.description)
    except PublisherError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"updated": True}


@router.post("/{item_id}/regenerate")
async def regenerate_episode(item_id: str, req: RegenerateRequest):
    """
    POST /api/episodes/{id}/regenerate - lägger ett "Generera om"-jobb i kön.

    Jobbet (services/pipeline.py:_run_regenerate_job) tar fram ett nytt
    AI-förslag på titel och/eller beskrivning. Det skriver aldrig till
    tjänsten - förslaget hämtas via GET /api/process/status/{job_id} och
    fylls i i fliken, där användaren sparar (eller kopierar) det.

    Args:
        item_id: Tjänstens id, eller "h" + radens id i historiken.
        req: Vilka fält som ska genereras om.

    Returns:
        {"job_id", "queue_id"}.

    Raises:
        HTTPException 400: Om inget fält valts.
        HTTPException 403: Om avsnittet varken har ett sparat transkript
            eller kan laddas ner från tjänsten.
        HTTPException 404: Om ett historik-id inte finns.
    """
    if not req.regenerate_title and not req.regenerate_description:
        raise HTTPException(status_code=400, detail="Välj minst titel eller beskrivning att generera om.")

    publisher = get_publisher()
    history_id = episode_library.history_id_of(item_id)
    if history_id is not None:
        row = episode_store.get_published(history_id)
        if not row:
            raise HTTPException(status_code=404, detail="Avsnittet finns inte i historiken.")
        if episode_store.read_transcript(row) is None:
            raise HTTPException(
                status_code=403,
                detail="Transkriptet för avsnittet finns inte kvar, och ljudet kan inte hämtas från tjänsten.",
            )
        filename = row.get("title") or f"Avsnitt {item_id}"
        speaker = row.get("speaker") or ""
        fields = {"history_id": history_id}
    else:
        if not publisher.is_live():
            raise HTTPException(
                status_code=403,
                detail=f"{publisher.label} är inte inställt för att hantera publicerade avsnitt.",
            )
        cached = publisher.cached_episode(item_id)
        filename = (cached or {}).get("title") or f"Avsnitt {item_id}"
        speaker = (cached or {}).get("speaker") or ""
        # Spreaker-id:n är heltal - så har de alltid sparats i kön.
        fields = {"episode_id": int(item_id) if item_id.isdigit() else item_id}

    job_id = str(uuid.uuid4())
    queue_id = str(uuid.uuid4())
    fields.update({
        "regenerate_title": req.regenerate_title,
        "regenerate_description": req.regenerate_description,
        "force_retranscribe": req.force_retranscribe,
    })
    # original_path/keep_original är inte meningsfulla för den här kinden -
    # _run_regenerate_job hämtar transkriptet eller ljudet själv.
    queue_store.add(
        queue_id, job_id, "regenerate", filename, speaker, fields,
        Path(f"episode-{item_id}"), False, datetime.now().isoformat(),
    )
    return {"job_id": job_id, "queue_id": queue_id}
