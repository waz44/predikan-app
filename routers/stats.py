"""
Router: stats
Ackumulerad prestandastatistik, se modules/episode_store.py.
"""
from fastapi import APIRouter

from modules import episode_store

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats")
async def get_processing_stats():
    """
    Ackumulerad prestandastatistik över alla lyckade bearbetningar
    (se modules/episode_store.py): totalt antal, total predikantid, total
    bearbetningstid och kvoten mellan dem (processing_ratio). Används av
    frontend för att uppskatta bearbetningstid för nästa predikan.
    """
    return episode_store.get_stats()
