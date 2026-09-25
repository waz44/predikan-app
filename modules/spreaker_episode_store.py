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
# re: hitta raden "Talare: ..." i beskrivningen.
import re

# datetime/UTC: tidsstämpeln för när listan hämtades (lagras i UTC).
from datetime import UTC, datetime

# db.get_connection() ger en kortlivad SQLite-anslutning per anrop.
from modules import db

# En rad som börjar med "Talare:" (oavsett versaler), följt av namnet.
# MULTILINE gör att ^ och $ gäller varje rad i beskrivningen, inte bara
# början och slutet av hela texten.
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
    # Samma tidsstämpel för alla rader - de hämtades i samma omgång.
    fetched_at = datetime.now(UTC).isoformat()

    # Allt sker i en och samma transaktion (ett with-block): misslyckas en
    # rad ångras även borttagningen, så listan aldrig blir halvtom.
    with db.get_connection() as conn:
        conn.execute("DELETE FROM spreaker_episodes")
        for ep in raw_episodes:
            description = ep.get("description")
            # Spreaker anger längden i millisekunder - lagras i sekunder.
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
    """
    Alla cachade avsnitt, senast publicerade först. Innehåller has_transcript
    (härledd via en LEFT JOIN mot spreaker_transcripts) så frontend kan visa
    om "Generera om" kan återanvända ett redan nedladdat/transkriberat
    avsnitt istället för att transkribera på nytt.
    """
    with db.get_connection() as conn:
        # LEFT JOIN: alla avsnitt kommer med, och has_transcript blir 1 för
        # dem som har en rad i spreaker_transcripts, annars 0.
        rows = conn.execute(
            """
            SELECT e.*, (t.episode_id IS NOT NULL) AS has_transcript
            FROM spreaker_episodes e
            LEFT JOIN spreaker_transcripts t ON t.episode_id = e.episode_id
            ORDER BY e.published_at DESC
            """
        ).fetchall()
    # sqlite3.Row -> vanliga dicts, som kan skickas som JSON till webbsidan.
    return [dict(row) for row in rows]


def get(episode_id: int) -> dict | None:
    """
    En enskild cachad rad, t.ex. för att visa nuvarande titel/talare när ett köobjekt skapas (se routers/spreaker_episodes.py).

    Args:
        episode_id: Spreakers id för avsnittet.

    Returns:
        Raden som dict (episode_id, title, description, speaker,
        published_at, duration_seconds, plays_count, site_url, fetched_at),
        eller None om avsnittet inte finns i den lokala listan.
    """
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM spreaker_episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()
    return dict(row) if row else None


def get_transcript(episode_id: int) -> str | None:
    """
    Ett tidigare nedladdat/transkriberat avsnitts transkript, om det redan finns cachat (se save_transcript).

    Args:
        episode_id: Spreakers id för avsnittet.

    Returns:
        Transkriptets text, eller None om avsnittet aldrig transkriberats.
    """
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT transcript FROM spreaker_transcripts WHERE episode_id = ?", (episode_id,)
        ).fetchone()
    return row["transcript"] if row else None


def save_transcript(episode_id: int, transcript: str) -> None:
    """
    Sparar (eller ersätter) det cachade transkriptet för ett avsnitt, så nästa 'Generera om' slipper transkribera på nytt.

    Args:
        episode_id: Spreakers id för avsnittet.
        transcript: Hela transkriptet. Ett befintligt transkript skrivs över
            (t.ex. efter "Transkribera om" med en bättre modell).
    """
    # "Upsert": finns avsnittet redan ersätts transkriptet och tidsstämpeln,
    # annars skapas en ny rad.
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO spreaker_transcripts (episode_id, transcript, transcribed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(episode_id) DO UPDATE SET
                transcript = excluded.transcript,
                transcribed_at = excluded.transcribed_at
            """,
            (episode_id, transcript, datetime.now(UTC).isoformat()),
        )


def update_local(episode_id: int, title: str, description: str) -> None:
    """
    Uppdaterar titel/beskrivning/omtolkad talare för EN cachad rad efter en lyckad sparning mot Spreaker.

    Lokala listan hålls därmed i synk utan att hela listan behöver hämtas
    om från Spreaker. Talaren läses ut på nytt ur den nya beskrivningen.

    Args:
        episode_id: Spreakers id för avsnittet.
        title: Den nya titeln.
        description: Den nya beskrivningen.
    """
    # Talaren räknas om ur den nya beskrivningen, så Talare-kolumnen
    # stämmer direkt efter en sparning.
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE spreaker_episodes SET title = ?, description = ?, speaker = ? WHERE episode_id = ?",
            (title, description, _extract_speaker(description), episode_id),
        )
