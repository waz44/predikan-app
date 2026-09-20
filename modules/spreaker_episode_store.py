"""
Modul: spreaker_episode_store
Databaslager för den lokala CACHEN av vad som faktiskt ligger på det
riktiga Spreaker-kontot (tabellen spreaker_episodes, se modules/db.py).
Helt separat från modules/episode_store.py, som bara loggar appens EGNA
lyckade publiceringar för statistik/rensning.

Cachen finns för att "Hantera Spreaker"-fliken i frontend (routers/spreaker_episodes.py)
ska kunna visa/sortera avsnittslistan snabbt utan att göra ett nytt
API-anrop mot Spreaker vid varje sidvisning - den fylls om helt via
replace_all() varje gång användaren klickar "Hämta från Spreaker".
"""
import re
from datetime import UTC, datetime

from modules import db

_SPEAKER_LINE = re.compile(r"^Talare:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _extract_speaker(description: str | None) -> str | None:
    """
    Plockar ut talaren ur en beskrivning, om en rad på formen "Talare: X"
    finns (samma konvention som services/pipeline.py använder när appen
    själv bygger en beskrivning - se _run_processing_job). Om flera
    matchande rader finns (t.ex. efter manuell redigering) används SISTA
    raden, precis som konventionen är dokumenterad.
    """
    if not description:
        return None
    matches = _SPEAKER_LINE.findall(description)
    if not matches:
        return None
    return matches[-1].strip() or None


def replace_all(raw_episodes: list[dict]) -> None:
    """
    Tömmer och fyller om hela cachen i EN transaktion, utifrån Spreakers
    API-svar (modules/spreaker_client.py:list_episodes). Full ersättning
    (inte en upsert) så att avsnitt som tagits bort på kontot sen förra
    hämtningen försvinner ur cachen också.

    Fälten hämtas defensivt med .get(...) - Spreakers dokumenterade
    listningssvar visar inte säkert samma fullständiga fältuppsättning som
    ett enskilt avsnitt (t.ex. description/plays_count), så saknade fält
    blir bara tomma istället för att krascha hämtningen.
    """
    fetched_at = datetime.now(UTC).isoformat()

    with db.get_connection() as conn:
        conn.execute("DELETE FROM spreaker_episodes")
        for ep in raw_episodes:
            description = ep.get("description")
            duration_ms = ep.get("duration")
            conn.execute(
                """
                INSERT INTO spreaker_episodes (
                    episode_id, title, description, speaker, published_at,
                    duration_seconds, plays_count, site_url, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ep["episode_id"],
                    ep.get("title") or "",
                    description,
                    _extract_speaker(description),
                    ep.get("published_at"),
                    (duration_ms / 1000) if duration_ms is not None else None,
                    ep.get("plays_count"),
                    ep.get("site_url"),
                    fetched_at,
                ),
            )


def get_all() -> list[dict]:
    """Alla cachade avsnitt, senast publicerade först."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM spreaker_episodes ORDER BY published_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def update_local(episode_id: int, title: str, description: str) -> None:
    """Uppdaterar titel/beskrivning/omtolkad talare för EN cachad rad efter en lyckad sparning mot Spreaker."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE spreaker_episodes SET title = ?, description = ?, speaker = ? WHERE episode_id = ?",
            (title, description, _extract_speaker(description), episode_id),
        )
