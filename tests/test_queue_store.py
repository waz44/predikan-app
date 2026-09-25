"""Enhetstester för modules/queue_store.py - den beständiga bearbetningskön."""
from pathlib import Path

from modules import queue_store


def _add(tmp_env, queue_id, job_id, speaker, kind="manual"):
    """
    Lägger ett objekt i kön med påhittade men rimliga värden.

    Args:
        tmp_env: Testmiljön (temporära mappar och databas).
        queue_id: Radens id i kön.
        job_id: Jobbets id.
        speaker: Talarens namn - används också som filnamn.
        kind: "manual" eller "bulk" (bulk ger keep_original=True).
    """
    queue_store.add(
        queue_id, job_id, kind, f"{speaker}.mp3", speaker,
        {"start_seconds": 0, "end_seconds": 10},
        tmp_env["upload_dir"] / f"{speaker}.mp3",
        kind == "bulk",
        "2026-01-01T00:00:00",
    )


def test_add_and_get_all_preserves_order(tmp_env):
    """
    Objekt kommer tillbaka i den ordning de lades till, med status "queued"
    och sökvägen som ett Path-objekt (inte som text från databasen).
    """
    _add(tmp_env, "q1", "j1", "Anna")
    _add(tmp_env, "q2", "j2", "Bertil")
    _add(tmp_env, "q3", "j3", "Cecilia")

    items = queue_store.get_all()
    assert [it["speaker"] for it in items] == ["Anna", "Bertil", "Cecilia"]
    assert items[0]["status"] == "queued"
    assert isinstance(items[0]["original_path"], Path)


def test_next_queued_returns_first_waiting_item(tmp_env):
    """
    Arbetartråden ska alltid få det FÖRSTA väntande objektet - och ett objekt
    som redan körs räknas inte som väntande.
    """
    _add(tmp_env, "q1", "j1", "Anna")
    _add(tmp_env, "q2", "j2", "Bertil")

    assert queue_store.next_queued_unless_paused()["speaker"] == "Anna"

    queue_store.set_running("j1")
    # "Anna" är nu 'running', inte 'queued' - nästa väntande ska vara Bertil
    assert queue_store.next_queued_unless_paused()["speaker"] == "Bertil"


def test_next_queued_skips_when_paused(tmp_env):
    """
    Pausad kö: inget objekt lämnas ut. Startad igen: samma objekt kommer tillbaka.
    """
    _add(tmp_env, "q1", "j1", "Anna")
    queue_store.set_paused(True)
    assert queue_store.next_queued_unless_paused() is None
    queue_store.set_paused(False)
    assert queue_store.next_queued_unless_paused()["speaker"] == "Anna"


def test_move_to_front_only_affects_queued_items(tmp_env):
    """
    "Prioritera" flyttar ett väntande objekt först i kön, men ett objekt som
    redan körs kan inte flyttas.
    """
    _add(tmp_env, "q1", "j1", "Anna")
    _add(tmp_env, "q2", "j2", "Bertil")
    _add(tmp_env, "q3", "j3", "Cecilia")

    assert queue_store.move_to_front("q3") is True
    order = [it["speaker"] for it in queue_store.get_all()]
    assert order == ["Cecilia", "Anna", "Bertil"]

    queue_store.set_running("j2")
    assert queue_store.move_to_front("q2") is False, "ett pågående objekt ska inte kunna prioriteras om"


def test_set_finished_stores_result_and_error(tmp_env):
    """
    Ett klart jobb sparar status, resultat (som JSON) och procent, och har
    inget felmeddelande.
    """
    _add(tmp_env, "q1", "j1", "Anna")
    queue_store.set_running("j1")

    queue_store.set_finished("j1", "done", result={"final_title": "Titel"}, overall_percent=100)
    row = queue_store.get_by_job_id("j1")
    assert row["status"] == "done"
    assert row["result"] == {"final_title": "Titel"}
    assert row["overall_percent"] == 100
    assert row["error"] is None


def test_remove_refuses_nothing_itself_but_caller_should_guard_running(tmp_env):
    """queue_store.remove() är en ren rad-borttagning utan statuskoll - den kollen görs i routers/queue.py."""
    _add(tmp_env, "q1", "j1", "Anna")
    assert queue_store.remove("q1") is True
    assert queue_store.get("q1") is None
    assert queue_store.remove("q1") is False, "redan borttagen rad ska returnera False, inte krascha"


def test_remove_where_status_in_and_not_in(tmp_env):
    """
    "Rensa fel/avbrutna" tar bort bara felaktiga rader, och "Rensa allt"
    tar bort allt UTOM det som körs just nu.
    """
    _add(tmp_env, "q1", "j1", "Anna")
    _add(tmp_env, "q2", "j2", "Bertil")
    _add(tmp_env, "q3", "j3", "Cecilia")
    queue_store.set_running("j1")
    queue_store.set_finished("j2", "error", error="Något gick fel")

    removed = queue_store.remove_where_status_in(["error"])
    assert removed == 1
    assert queue_store.get_by_job_id("j2") is None

    removed = queue_store.remove_where_status_not_in(["running"])
    assert removed == 1  # bara Cecilia (queued) - Anna (running) ska överleva
    remaining = [it["speaker"] for it in queue_store.get_all()]
    assert remaining == ["Anna"]


def test_reset_stale_running_marks_orphaned_jobs_as_error(tmp_env):
    """
    Vid omstart markeras jobb som stod som "running" som fel (arbetet dog med
    den gamla processen) - väntande jobb lämnas orörda.
    """
    _add(tmp_env, "q1", "j1", "Anna")
    _add(tmp_env, "q2", "j2", "Bertil")
    queue_store.set_running("j1")
    # "Bertil" lämnas 'queued' - ska INTE påverkas av reset_stale_running

    count = queue_store.reset_stale_running("Avbruten av omstart.")
    assert count == 1

    anna = queue_store.get_by_job_id("j1")
    assert anna["status"] == "error"
    assert anna["error"] == "Avbruten av omstart."

    bertil = queue_store.get_by_job_id("j2")
    assert bertil["status"] == "queued"


def test_paused_state_defaults_to_false_and_persists(tmp_env):
    """
    Kön är inte pausad från början, och pausläget sparas i databasen.
    """
    assert queue_store.get_paused() is False
    queue_store.set_paused(True)
    assert queue_store.get_paused() is True
    queue_store.set_paused(False)
    assert queue_store.get_paused() is False


def test_set_end_seconds_updates_fields_blob(tmp_env):
    """
    Bulkimport sätter slutpunkten (filens längd) först när jobbet startar -
    värdet ska hamna i den sparade JSON-texten med formulärfälten.
    """
    _add(tmp_env, "q1", "j1", "Anna", kind="bulk")
    queue_store.set_end_seconds("j1", 123.4)
    row = queue_store.get_by_job_id("j1")
    assert row["fields"]["end_seconds"] == 123.4
