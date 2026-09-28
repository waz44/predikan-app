"""
Tester för schemalagd arkivering (modules/archive_scheduler.py) och dess
inställningar i routers/setup.py.
"""
import threading
from datetime import datetime

import config
from modules import archive_scheduler, podcast_archive

# En onsdag.
NOW = datetime(2026, 9, 30, 14, 0)


def test_next_run_daily():
    # Senare i dag om tiden inte passerats, annars i morgon.
    assert archive_scheduler.next_run(NOW, "daily", 0, "22:30") == datetime(2026, 9, 30, 22, 30)
    assert archive_scheduler.next_run(NOW, "daily", 0, "03:00") == datetime(2026, 10, 1, 3, 0)
    # Exakt nu räknas som passerat - nästa körning är i morgon.
    assert archive_scheduler.next_run(NOW, "daily", 0, "14:00") == datetime(2026, 10, 1, 14, 0)


def test_next_run_weekly():
    # Söndag (6) samma vecka, måndag (0) nästa vecka.
    assert archive_scheduler.next_run(NOW, "weekly", 6, "03:00") == datetime(2026, 10, 4, 3, 0)
    assert archive_scheduler.next_run(NOW, "weekly", 0, "03:00") == datetime(2026, 10, 5, 3, 0)
    # Samma veckodag: i dag om tiden är kvar, annars om en vecka.
    assert archive_scheduler.next_run(NOW, "weekly", 2, "20:00") == datetime(2026, 9, 30, 20, 0)
    assert archive_scheduler.next_run(NOW, "weekly", 2, "08:00") == datetime(2026, 10, 7, 8, 0)


def test_next_run_off_and_bad_time():
    assert archive_scheduler.next_run(NOW, "off", 0, "03:00") is None
    # Ett oläsligt klockslag ger standardtiden 03:00.
    assert archive_scheduler.next_run(NOW, "daily", 0, "kl tre") == datetime(2026, 10, 1, 3, 0)


def test_loop_starts_archive_when_due(monkeypatch):
    """Är det dags startas en vanlig arkivkörning, och nästa tid räknas fram."""
    monkeypatch.setattr(config, "ARCHIVE_SCHEDULE", "daily")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "123")
    started = []
    monkeypatch.setattr(podcast_archive, "start", lambda: started.append(True) or True)
    # Första beräkningen ger en tid som redan passerats, nästa en i framtiden.
    times = iter([datetime(2000, 1, 1), datetime(2999, 1, 1)])
    monkeypatch.setattr(archive_scheduler, "next_run", lambda now, *args: next(times))

    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda timeout: True)  # bara ett varv
    archive_scheduler.run_loop(stop)

    assert started == [True]


def test_schedule_settings_are_validated(client, monkeypatch):
    from routers import setup

    written = {}
    monkeypatch.setattr(setup.env_file, "set_values", lambda updates: written.update(updates))
    monkeypatch.setattr(setup.config, "reload", lambda: None)

    ok = client.post("/api/setup/save", json={"values": {
        "ARCHIVE_SCHEDULE": "weekly", "ARCHIVE_SCHEDULE_DAY": "6", "ARCHIVE_SCHEDULE_TIME": "03:30",
    }})
    assert ok.status_code == 200
    assert written["ARCHIVE_SCHEDULE_TIME"] == "03:30"

    for values in ({"ARCHIVE_SCHEDULE": "monthly"}, {"ARCHIVE_SCHEDULE_DAY": "7"}, {"ARCHIVE_SCHEDULE_TIME": "25:00"}):
        assert client.post("/api/setup/save", json={"values": values}).status_code == 400


def test_archive_status_includes_next_run(client, monkeypatch):
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "123")
    monkeypatch.setattr(config, "ARCHIVE_SCHEDULE", "daily")
    monkeypatch.setattr(config, "ARCHIVE_SCHEDULE_TIME", "03:00")
    data = client.get("/api/spreaker/archive/status").json()
    assert data["next_scheduled_run"].endswith("T03:00")

    monkeypatch.setattr(config, "ARCHIVE_SCHEDULE", "off")
    assert client.get("/api/spreaker/archive/status").json()["next_scheduled_run"] is None
