"""
Modul: stats
Håller enkla, ackumulerade prestandamått för bearbetningspipelinen, sparade
i en JSON-fil (config.STATS_FILE) så de överlever omstarter av servern:

- total_processing_seconds: total tid bearbetningen (klippning t.o.m.
  Spreaker-publicering) faktiskt tagit, summerat över alla predikningar
- total_sermon_seconds: total ljudlängd på de predikningar som bearbetats
- total_count: antal genomförda bearbetningar

Kvoten total_processing_seconds / total_sermon_seconds ("processing_ratio")
är bearbetningssekunder per predikan-sekund, och används för att uppskatta
hur lång tid nästa predikan tar att bearbeta, proportionellt mot dess
längd (t.ex. tar en 38-minuters predikan 20 minuter, uppskattas en
19-minuters predikan på samma system ta ca 10 minuter).

Endast lyckade bearbetningar räknas in - se app.py:_run_processing_job.
"""
import json
import threading
from pathlib import Path

_lock = threading.Lock()

_DEFAULTS = {
    "total_processing_seconds": 0.0,
    "total_sermon_seconds": 0.0,
    "total_count": 0,
}


def _load(stats_path: Path) -> dict:
    if stats_path.exists():
        try:
            data = json.loads(stats_path.read_text(encoding="utf-8"))
            return {**_DEFAULTS, **data}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULTS)


def record_job(stats_path: Path, processing_seconds: float, sermon_seconds: float) -> dict:
    """Lägger till en lyckad bearbetning i statistiken och sparar den till disk."""
    with _lock:
        data = _load(stats_path)
        data["total_processing_seconds"] += max(0.0, processing_seconds)
        data["total_sermon_seconds"] += max(0.0, sermon_seconds)
        data["total_count"] += 1
        stats_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data


def get_stats(stats_path: Path) -> dict:
    """Returnerar ackumulerad statistik plus beräknad processing_ratio (None om ingen historik finns än)."""
    with _lock:
        data = _load(stats_path)

    ratio = (
        data["total_processing_seconds"] / data["total_sermon_seconds"]
        if data["total_sermon_seconds"] > 0
        else None
    )
    data["processing_ratio"] = ratio
    return data


def estimate_processing_seconds(stats_path: Path, sermon_seconds: float, fallback_ratio: float = 1.0) -> float:
    """
    Uppskattar bearbetningstid (sekunder) för en predikan av given längd,
    baserat på historiskt snitt (processing_ratio). Innan någon historik
    finns används `fallback_ratio` som en grov gissning.
    """
    ratio = get_stats(stats_path)["processing_ratio"] or fallback_ratio
    return max(0.0, sermon_seconds) * ratio
