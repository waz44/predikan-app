"""
Router: meta
Enkla metadata-endpoints om själva appen. Just nu bara versionen (se
config.VERSION, som läses från EN källa - pyproject.toml).
"""
from fastapi import APIRouter

import config

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/version")
async def get_version():
    """
    GET /api/version - appens versionsnummer.

    Versionen läses från pyproject.toml (se config.VERSION), så den alltid
    stämmer med den publicerade releasen.

    Returns:
        {"version": "1.2.1"} (till exempel).
    """
    return {"version": config.VERSION}
