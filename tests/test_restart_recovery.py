"""
Verifierar hela poängen med att flytta kön till en databas: att den
överlever en omstart av servern. Simulerar en omstart genom att stänga
och öppna en ny TestClient (vilket kör appens riktiga
startup/shutdown-hookar, se app.py) mot samma temporära databas.
"""
from fastapi.testclient import TestClient

import app as app_module
from modules import db, queue_store


def test_queued_items_and_pause_state_survive_restart(tmp_env):
    with TestClient(app_module.app) as client:
        client.post("/api/queue/pause")
        csv_bytes = b"filnamn,talare,datum,klockslag\na.mp3,Anna,2026-01-01,10:00\n"
        client.post("/api/bulk-import", files={"file": ("t.csv", csv_bytes, "text/csv")})

        q = client.get("/api/queue").json()
        assert q["paused"] is True
        assert len(q["items"]) == 1
        assert q["items"][0]["status"] == "queued"

    # "Omstart": en helt ny TestClient-kontext mot samma databasfil.
    with TestClient(app_module.app) as client:
        q = client.get("/api/queue").json()
        assert q["paused"] is True, "pausläget ska ha överlevt omstarten"
        assert len(q["items"]) == 1
        assert q["items"][0]["speaker"] == "Anna"
        assert q["items"][0]["status"] == "queued", "ett väntande objekt ska ligga kvar oförändrat"


def test_orphaned_running_item_reset_to_error_on_restart(tmp_env):
    with TestClient(app_module.app) as client:
        # Kön pausas FÖRST så den riktiga (levande) kö-arbetartråden i den
        # här processen inte hinner plocka upp och bearbeta objektet på
        # riktigt innan testet självt hunnit tvinga det till 'running' -
        # annars kapplöper testet mot sin egen bakgrundstråd.
        client.post("/api/queue/pause")

        # Simulerar att servern kraschade mitt i ett jobb: raden hann
        # aldrig bli 'done'/'error'/'cancelled' innan processen dog.
        queue_store.add(
            "q1", "j1", "manual", "a.mp3", "Anna",
            {"start_seconds": 0, "end_seconds": 10},
            tmp_env["upload_dir"] / "a.mp3", False, "2026-01-01T00:00:00",
        )
        queue_store.set_running("j1")

    with TestClient(app_module.app) as client:
        q = client.get("/api/queue").json()
        item = q["items"][0]
        assert item["status"] == "error"
        assert "omstart" in item["error"].lower()


def test_init_db_is_idempotent(tmp_env):
    """db.init_db() ska kunna köras flera gånger (t.ex. varje appstart) utan att klaga eller tömma data."""
    db.init_db()
    queue_store.add(
        "q1", "j1", "manual", "a.mp3", "Anna",
        {"start_seconds": 0, "end_seconds": 10},
        tmp_env["upload_dir"] / "a.mp3", False, "2026-01-01T00:00:00",
    )
    db.init_db()
    assert queue_store.get("q1") is not None
