"""
Modul: queue_store
Databaslager för bearbetningskön (SQLite-tabellen queue_items, se
modules/db.py). Ersätter de tidigare in-memory-strukturerna QUEUE/JOBS i
app.py, så att kön - vilka predikningar som väntar, i vilken ordning, och
resultatet av redan avslutade - överlever en omstart av servern.

Live per-steg-procent (den som tickar var 0,4:e sekund under bearbetning)
skrivs MEDVETET inte hit - det hade inneburit flera databasskrivningar per
sekund för ren UI-animation utan verkligt värde. Den hålls istället i en
enkel in-memory-dict i app.py (RUNNING_PROGRESS) och slås ihop med raden
härifrån först i API-svaret. Det enda som tappas vid en krasch mitt i ett
jobb är alltså den detaljerade steg-vyn för just det jobbet - resultatet av
redan avslutade jobb och alla väntande jobb finns kvar.
"""
import json
from pathlib import Path
from typing import Any

from modules import db

# Statusar ett köobjekt kan ha. "running" plockas aldrig upp igen efter en
# omstart (se reset_stale_running) eftersom det arbete som pågick dog med
# processen - det finns inget säkert sätt att återuppta det.
TERMINAL_STATUSES = ("done", "error", "cancelled")


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "queue_id": row["queue_id"],
        "job_id": row["job_id"],
        "kind": row["kind"],
        "filename": row["filename"],
        "speaker": row["speaker"],
        "fields": json.loads(row["fields"]),
        "original_path": Path(row["original_path"]),
        "keep_original": bool(row["keep_original"]),
        "status": row["status"],
        "base_name": row["base_name"],
        "overall_percent": row["overall_percent"],
        "error": row["error"],
        "result": json.loads(row["result"]) if row["result"] else None,
        "position": row["position"],
        "queued_at": row["queued_at"],
    }


def add(
    queue_id: str,
    job_id: str,
    kind: str,
    filename: str,
    speaker: str,
    fields: dict,
    original_path: Path,
    keep_original: bool,
    queued_at: str,
) -> None:
    with db.get_connection() as conn:
        next_position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM queue_items").fetchone()[0]
        conn.execute(
            """
            INSERT INTO queue_items
                (queue_id, job_id, kind, filename, speaker, fields, original_path,
                 keep_original, status, position, queued_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                queue_id,
                job_id,
                kind,
                filename,
                speaker,
                json.dumps(fields),
                str(original_path),
                int(keep_original),
                next_position,
                queued_at,
            ),
        )


def get_all() -> list[dict]:
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM queue_items ORDER BY position").fetchall()
    return [_row_to_dict(r) for r in rows]


def get(queue_id: str) -> dict | None:
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM queue_items WHERE queue_id = ?", (queue_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_by_job_id(job_id: str) -> dict | None:
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM queue_items WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def next_queued() -> dict | None:
    """Hämtar det objekt som väntar (status='queued') längst fram i kön, eller None."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM queue_items WHERE status = 'queued' ORDER BY position LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def set_running(job_id: str) -> None:
    with db.get_connection() as conn:
        conn.execute("UPDATE queue_items SET status = 'running' WHERE job_id = ?", (job_id,))


def set_base_name_and_path(job_id: str, base_name: str, original_path: Path) -> None:
    """Anropas när originalfilen flyttats/kopierats till uploads/ under sitt basnamn."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE queue_items SET base_name = ?, original_path = ? WHERE job_id = ?",
            (base_name, str(original_path), job_id),
        )


def set_end_seconds(job_id: str, end_seconds: float) -> None:
    """Uppdaterar 'fields'-blobens end_seconds (bulkimport känner bara ljudlängden precis innan bearbetning)."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT fields FROM queue_items WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            return
        fields = json.loads(row["fields"])
        fields["end_seconds"] = end_seconds
        conn.execute("UPDATE queue_items SET fields = ? WHERE job_id = ?", (json.dumps(fields), job_id))


def set_finished(
    job_id: str,
    status: str,
    error: str | None = None,
    result: dict | None = None,
    overall_percent: int | None = None,
) -> None:
    """status: 'done' | 'error' | 'cancelled'."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE queue_items SET status = ?, error = ?, result = ?, overall_percent = COALESCE(?, overall_percent) "
            "WHERE job_id = ?",
            (status, error, json.dumps(result) if result is not None else None, overall_percent, job_id),
        )


def remove(queue_id: str) -> bool:
    with db.get_connection() as conn:
        cur = conn.execute("DELETE FROM queue_items WHERE queue_id = ?", (queue_id,))
    return cur.rowcount > 0


def remove_where_status_in(statuses: list[str]) -> int:
    with db.get_connection() as conn:
        placeholders = ",".join("?" for _ in statuses)
        cur = conn.execute(f"DELETE FROM queue_items WHERE status IN ({placeholders})", statuses)
    return cur.rowcount


def remove_where_status_not_in(statuses: list[str]) -> int:
    with db.get_connection() as conn:
        placeholders = ",".join("?" for _ in statuses)
        cur = conn.execute(f"DELETE FROM queue_items WHERE status NOT IN ({placeholders})", statuses)
    return cur.rowcount


def move_to_front(queue_id: str) -> bool:
    """Flyttar ett väntande (status='queued') objekt längst fram i kön. Returnerar False om objektet inte finns/inte väntar."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT status FROM queue_items WHERE queue_id = ?", (queue_id,)).fetchone()
        if not row or row["status"] != "queued":
            return False
        min_position = conn.execute("SELECT COALESCE(MIN(position), 0) FROM queue_items").fetchone()[0]
        conn.execute(
            "UPDATE queue_items SET position = ? WHERE queue_id = ?", (min_position - 1, queue_id)
        )
    return True


def reset_stale_running(message: str) -> int:
    """
    Körs en gång vid appstart: allt som fortfarande är markerat 'running'
    kan bara bero på att servern avslutades onaturligt förra gången (en
    riktig avslutning hinner alltid bli 'done'/'error'/'cancelled' -
    _run_queue_item i app.py kör det synkront i kö-arbetartråden). Sätts
    till 'error' med ett tydligt meddelande istället för att gissningsvis
    försöka återuppta ett jobb vars faktiska arbete redan dog med processen.
    """
    with db.get_connection() as conn:
        cur = conn.execute(
            "UPDATE queue_items SET status = 'error', error = ? WHERE status = 'running'", (message,)
        )
    return cur.rowcount


def get_paused() -> bool:
    """Om kön var pausad senast appen kördes - se app_state i modules/db.py."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT paused FROM app_state WHERE id = 1").fetchone()
    return bool(row["paused"]) if row else False


def set_paused(paused: bool) -> None:
    with db.get_connection() as conn:
        conn.execute("UPDATE app_state SET paused = ? WHERE id = 1", (int(paused),))
