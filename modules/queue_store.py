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
# json: formulärfälten (fields) och resultatet (result) lagras som JSON-text
# i databasen, eftersom de har olika innehåll för olika sorters jobb.
import json

# Path: sökvägar lagras som text i databasen men används som Path i koden.
from pathlib import Path

# Any: typen för värdena i en rad, som kan vara text, tal, dict m.m.
from typing import Any

# db.get_connection() ger en kortlivad SQLite-anslutning per anrop.
from modules import db

# Statusar ett köobjekt kan ha. "running" plockas aldrig upp igen efter en
# omstart (se reset_stale_running) eftersom det arbete som pågick dog med
# processen - det finns inget säkert sätt att återuppta det.
TERMINAL_STATUSES = ("done", "error", "cancelled")


def _row_to_dict(row) -> dict[str, Any]:
    """
    Gör om en databasrad till den dict som resten av appen arbetar med.

    Databasen lagrar allt som text och heltal. Här packas det upp igen:
    fields och result är JSON-text som blir dict, original_path blir ett
    Path-objekt och keep_original (0/1) blir True/False.

    Args:
        row: En rad ur queue_items (sqlite3.Row, åtkomlig med kolumnnamn).

    Returns:
        En vanlig dict med ett nyckelvärde per kolumn.
    """
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
    """
    Lägger till ett nytt objekt sist i kön, med status 'queued'.

    Args:
        queue_id: Unikt id för raden i kön (används av Ta bort/Prioritera).
        job_id: Unikt id för själva jobbet (används för status och Avbryt).
        kind: "manual", "bulk" eller "regenerate".
        filename: Namn som visas i kön (filnamn eller avsnittets titel).
        speaker: Talarens namn, visas i kön.
        fields: Formulärets värden (sparas som JSON och blir ProcessRequest).
        original_path: Ljudfilen som ska bearbetas.
        keep_original: True = kopiera i stället för att flytta filen.
        queued_at: Tidsstämpel (ISO) när objektet köades.
    """
    with db.get_connection() as conn:
        # Nästa lediga position = högsta nuvarande + 1. COALESCE ger -1 när
        # kön är tom (MAX blir då NULL), så första objektet får position 0.
        next_position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM queue_items").fetchone()[0]
        # Frågetecknen fylls i av SQLite i tur och ordning. Värdena sätts
        # aldrig in i SQL-texten själva - så ett filnamn med t.ex. ett
        # citattecken kan aldrig ändra frågan (skydd mot SQL-injektion).
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
    """
    Alla objekt i kön, i köordning (lägst position först).

    Returns:
        En lista med dicts, samma format som _row_to_dict().
    """
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM queue_items ORDER BY position").fetchall()
    return [_row_to_dict(r) for r in rows]


def get(queue_id: str) -> dict | None:
    """
    Ett objekt i kön, utifrån dess queue_id.

    Args:
        queue_id: Radens id i kön.

    Returns:
        Objektet som dict, eller None om det inte finns.
    """
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM queue_items WHERE queue_id = ?", (queue_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_by_job_id(job_id: str) -> dict | None:
    """
    Ett objekt i kön, utifrån dess job_id.

    Frontend frågar om status med job_id (GET /api/process/status/{job_id}),
    medan kö-operationer använder queue_id - därför finns båda uppslagen.

    Args:
        job_id: Jobbets id.

    Returns:
        Objektet som dict, eller None om det inte finns.
    """
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM queue_items WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def next_queued_unless_paused() -> dict | None:
    """
    Hämtar det objekt som väntar (status='queued') längst fram i kön, men
    hoppar över hämtningen om kön är pausad - i EN och samma SQL-fråga, så
    paus-kollen och hämtningen inte kan hamna på varsin sida om ett
    pausa+lägg-till som sker mellan dem.

    Används av services/pipeline.py:queue_worker_loop (som tidigare gjorde
    detta som två separata anrop - get_paused() följt av en separat
    "hämta nästa"-fråga). Det TOCTOU-fönstret mellan de två anropen var normalt
    försumbart litet, men kunde under belastning bli tillräckligt brett för
    att arbetartråden skulle hinna se kön som opausad och plocka upp ett
    objekt som just lagts till (men vars övriga fält inte hunnit sättas
    klart av anroparen än) - se tests/test_restart_recovery.py:
    test_orphaned_running_item_reset_to_error_on_restart, som pausar kön
    FÖRST just för att skydda sig mot detta, men som ändå kunde kapplöpa
    mot den levande arbetartråden om paus-kollen och hämtningen inte var
    atomära.
    """
    with db.get_connection() as conn:
        # Underfrågan läser pausflaggan i samma fråga som hämtningen, så
        # båda ser exakt samma ögonblicksbild av databasen. Pausad kö ger
        # inga rader alls, och arbetartråden väntar då en stund.
        row = conn.execute(
            """
            SELECT * FROM queue_items
            WHERE status = 'queued'
              AND (SELECT paused FROM app_state WHERE id = 1) = 0
            ORDER BY position LIMIT 1
            """
        ).fetchone()
    return _row_to_dict(row) if row else None


def set_running(job_id: str) -> None:
    """
    Markerar ett jobb som pågående (status 'running').

    Args:
        job_id: Jobbets id.
    """
    with db.get_connection() as conn:
        conn.execute("UPDATE queue_items SET status = 'running' WHERE job_id = ?", (job_id,))


def set_base_name_and_path(job_id: str, base_name: str, original_path: Path) -> None:
    """
    Anropas när originalfilen flyttats/kopierats till uploads/ under sitt basnamn.

    Basnamnet behövs för att senare hitta och städa bort jobbets filer i
    uploads/ och processed/ om det avbryts eller misslyckas.

    Args:
        job_id: Jobbets id.
        base_name: Gemensamt basnamn för jobbets filer.
        original_path: Originalfilens nya plats i uploads/.
    """
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE queue_items SET base_name = ?, original_path = ? WHERE job_id = ?",
            (base_name, str(original_path), job_id),
        )


def set_end_seconds(job_id: str, end_seconds: float) -> None:
    """
    Uppdaterar 'fields'-blobens end_seconds (bulkimport känner bara ljudlängden precis innan bearbetning).

    Args:
        job_id: Jobbets id.
        end_seconds: Ljudfilens längd i sekunder.
    """
    with db.get_connection() as conn:
        # Läs, ändra och skriv tillbaka JSON-texten i samma anslutning (och
        # därmed samma transaktion), så ingen annan ändring hinner emellan.
        row = conn.execute("SELECT fields FROM queue_items WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            # Raden har tagits bort under tiden - inget att uppdatera.
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
    """
    Markerar ett jobb som avslutat och sparar resultat eller felmeddelande.

    Args:
        job_id: Jobbets id.
        status: 'done' | 'error' | 'cancelled'.
        error: Felmeddelande som visas i kön (för 'error' och 'cancelled').
        result: Resultatet som visas i kön (för 'done'), sparas som JSON.
        overall_percent: Hur långt jobbet kom. None = behåll sparat värde.
    """
    with db.get_connection() as conn:
        # COALESCE(?, overall_percent): om inget nytt värde skickas (None)
        # behålls det som redan står i databasen.
        conn.execute(
            "UPDATE queue_items SET status = ?, error = ?, result = ?, overall_percent = COALESCE(?, overall_percent) "
            "WHERE job_id = ?",
            (status, error, json.dumps(result) if result is not None else None, overall_percent, job_id),
        )


def remove(queue_id: str) -> bool:
    """
    Tar bort ett objekt ur kön (knappen "✕ Ta bort").

    Args:
        queue_id: Radens id i kön.

    Returns:
        True om en rad togs bort, False om den inte fanns.
    """
    with db.get_connection() as conn:
        cur = conn.execute("DELETE FROM queue_items WHERE queue_id = ?", (queue_id,))
    # rowcount = antal rader som frågan påverkade (0 eller 1 här).
    return cur.rowcount > 0


def remove_where_status_in(statuses: list[str]) -> int:
    """
    Tar bort alla objekt som har någon av de angivna statusarna.

    Används av "Rensa klara" och "Rensa fel/avbrutna".

    Args:
        statuses: T.ex. ["error", "cancelled"].

    Returns:
        Antal borttagna rader.
    """
    with db.get_connection() as conn:
        # Ett frågetecken per status, t.ex. "?,?" för två statusar. Bara
        # frågetecknen byggs in i SQL-texten - själva värdena skickas separat.
        placeholders = ",".join("?" for _ in statuses)
        cur = conn.execute(f"DELETE FROM queue_items WHERE status IN ({placeholders})", statuses)
    return cur.rowcount


def remove_where_status_not_in(statuses: list[str]) -> int:
    """
    Tar bort alla objekt UTOM de som har någon av de angivna statusarna.

    Används av "Rensa allt" med ["running"], så att ett pågående jobb
    aldrig försvinner ur listan medan det körs.

    Args:
        statuses: Statusar som ska behållas.

    Returns:
        Antal borttagna rader.
    """
    with db.get_connection() as conn:
        # Samma teknik som ovan, men NOT IN: allt UTOM de angivna statusarna.
        placeholders = ",".join("?" for _ in statuses)
        cur = conn.execute(f"DELETE FROM queue_items WHERE status NOT IN ({placeholders})", statuses)
    return cur.rowcount


def move_to_front(queue_id: str) -> bool:
    """
    Flyttar ett väntande (status='queued') objekt längst fram i kön. Returnerar False om objektet inte finns/inte väntar.

    Positionen sätts till en lägre än den lägsta i kön. Negativa positioner
    är helt i sin ordning - bara den inbördes ordningen spelar roll.

    Args:
        queue_id: Radens id i kön.

    Returns:
        True om objektet flyttades.
    """
    with db.get_connection() as conn:
        # Bara väntande objekt kan prioriteras - ett pågående eller avslutat
        # jobb har inget att vinna på att flyttas.
        row = conn.execute("SELECT status FROM queue_items WHERE queue_id = ?", (queue_id,)).fetchone()
        if not row or row["status"] != "queued":
            return False
        # Lägsta position bland ALLA rader (även klara), så att objektet
        # hamnar först oavsett vad som ligger kvar i listan.
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
    """
    Om kön var pausad senast appen kördes - se app_state i modules/db.py.

    Returns:
        True om kön är pausad.
    """
    with db.get_connection() as conn:
        # app_state har alltid exakt en rad (id = 1), se modules/db.py.
        row = conn.execute("SELECT paused FROM app_state WHERE id = 1").fetchone()
    # SQLite saknar en riktig boolesk typ - pausflaggan lagras som 0 eller 1.
    return bool(row["paused"]) if row else False


def set_paused(paused: bool) -> None:
    """
    Pausar eller startar kön. Sparas i databasen, så pausläget överlever en omstart.

    Args:
        paused: True = pausa, False = starta.
    """
    with db.get_connection() as conn:
        conn.execute("UPDATE app_state SET paused = ? WHERE id = 1", (int(paused),))
