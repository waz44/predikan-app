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
    return {"version": config.VERSION}
