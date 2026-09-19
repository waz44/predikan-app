"""
Modul: episode_store
Databaslager för episodhistoriken (SQLite-tabellen episodes, se
modules/db.py). En rad skrivs per LYCKAD bearbetning (se
app.py:_run_queue_item). Ersätter två tidigare fristående, filbaserade
lösningar:

- modules/stats.py (stats.json) - statistiken beräknas nu istället som en
  aggregatfråga över episodes, se get_stats()/estimate_processing_seconds().
- Den filnamnsbaserade grupperingen i modules/storage_cleanup.py
  (enforce_retention) - vilka episoder som finns och i vilken ordning de
  skapades avgörs nu av databasen istället för att tolkas ur filnamn i
  uploads/+processed/, vilket tidigare av misstag missade en filtyp
  (AI-debugfiler) som inte följde den förväntade namnkonventionen.

Den faktiska filborttagningen vid rensning återanvänder ändå ett enkelt,
generiskt glob-mönster mot processed/ (fångar klippt ljud, transkript,
AI-berikning och eventuella framtida filtyper oavsett vad de heter) -
bara VILKA episoder som är "för gamla" kommer numera från databasen.
"""
import json
from glob import escape as glob_escape
from pathlib import Path

from modules import db

_DEFAULT_FALLBACK_RATIO = 1.0


def record_episode(data: dict) -> None:
    """Sparar en lyckat avslutad episod. `data` speglar formen på job["result"] plus lite extra (se app.py)."""
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO episodes (
                base_name, speaker, title, description, tags, publish_date, category, kind,
                episode_url, simulated, scheduled, backdated, email_sent,
                audio_path, transcript_path, enrichment_path,
                sermon_seconds, processing_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(base_name) DO UPDATE SET
                episode_url = excluded.episode_url,
                processing_seconds = excluded.processing_seconds
            """,
            (
                data["base_name"],
                data["speaker"],
                data.get("title"),
                data.get("description"),
                json.dumps(data.get("tags") or []),
                data.get("publish_date"),
                data.get("category"),
                data["kind"],
                data.get("episode_url"),
                int(bool(data.get("simulated"))),
                int(bool(data.get("scheduled"))),
                int(bool(data.get("backdated"))),
                int(bool(data.get("email_sent"))),
                data.get("audio_path"),
                data.get("transcript_path"),
                data.get("enrichment_path"),
                data["sermon_seconds"],
                data["processing_seconds"],
                data["created_at"],
            ),
        )


def get_stats() -> dict:
    """Ackumulerad statistik + processing_ratio (None om ingen historik finns än)."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total_count, "
            "COALESCE(SUM(sermon_seconds), 0) AS total_sermon_seconds, "
            "COALESCE(SUM(processing_seconds), 0) AS total_processing_seconds "
            "FROM episodes"
        ).fetchone()

    total_sermon_seconds = row["total_sermon_seconds"]
    total_processing_seconds = row["total_processing_seconds"]
    ratio = total_processing_seconds / total_sermon_seconds if total_sermon_seconds > 0 else None

    return {
        "total_count": row["total_count"],
        "total_sermon_seconds": total_sermon_seconds,
        "total_processing_seconds": total_processing_seconds,
        "processing_ratio": ratio,
    }


def estimate_processing_seconds(sermon_seconds: float, fallback_ratio: float = _DEFAULT_FALLBACK_RATIO) -> float:
    """Uppskattar bearbetningstid för en predikan av given längd, baserat på historiskt snitt (se get_stats)."""
    ratio = get_stats()["processing_ratio"] or fallback_ratio
    return max(0.0, sermon_seconds) * ratio


def enforce_retention(processed_dir: Path, max_episodes: int) -> list[str]:
    """
    Tar bort de äldsta episoderna (och deras filer) tills högst
    `max_episodes` återstår. Returnerar bas-filnamnen på de episoder som
    togs bort, så anroparen kan städa egna referenser (t.ex.
    UPLOADED_FILES i app.py).
    """
    if max_episodes <= 0:
        return []

    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT base_name, audio_path FROM episodes ORDER BY created_at DESC"
        ).fetchall()

    if len(rows) <= max_episodes:
        return []

    to_remove = rows[max_episodes:]
    removed_bases: list[str] = []

    with db.get_connection() as conn:
        for row in to_remove:
            base_name = row["base_name"]
            audio_path: str | None = row["audio_path"]

            if audio_path:
                try:
                    Path(audio_path).unlink(missing_ok=True)
                except OSError:
                    pass

            for p in processed_dir.glob(f"{glob_escape(base_name)}-*"):
                try:
                    p.unlink()
                except OSError:
                    pass

            conn.execute("DELETE FROM episodes WHERE base_name = ?", (base_name,))
            removed_bases.append(base_name)

    return removed_bases
