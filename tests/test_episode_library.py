"""
Tester för fliken Avsnitt: den sammanslagna listan (modules/episode_library.py),
routers/episodes.py och uppgraderingen av episodes-tabellen (modules/db.py).
Inga riktiga anrop mot Spreaker.
"""
import sqlite3
import time
from datetime import datetime

import config
from modules import ai_enrichment, db, episode_store, spreaker_episode_store


def _configure_real_spreaker(monkeypatch):
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "tok")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "123")
    monkeypatch.setattr(config, "SPREAKER_SIMULATE", False)


def _record(tmp_env, name, provider_id=None, simulated=False, transcript="Transkriptet."):
    """Lägger en publicering i appens historik, med ett transkript i processed/."""
    transcript_path = tmp_env["processed_dir"] / f"{name}-transcript.txt"
    if transcript is not None:
        transcript_path.write_text(transcript, encoding="utf-8")
    episode_store.record_episode({
        "base_name": name,
        "speaker": "Anna",
        "title": f"Anna: {name}",
        "description": f"Om {name}\n\nTalare: Anna",
        "kind": "manual",
        "episode_url": f"https://www.spreaker.com/episode/{name}--{provider_id}" if provider_id else "https://x/simulated",
        "simulated": simulated,
        "transcript_path": str(transcript_path),
        "sermon_seconds": 60.0,
        "processing_seconds": 10.0,
        "created_at": datetime.now().isoformat(),
        "provider": "spreaker",
        "provider_episode_id": provider_id,
    })


def _history_id(name):
    return next(row["id"] for row in episode_store.list_published() if row["base_name"] == name)


def test_old_database_gets_provider_columns_from_links(tmp_env):
    """
    En databas från före v1.5.0 får kolumnerna, och publicerade avsnitt får
    sitt Spreaker-id ur länken - men inte simulerade.
    """
    with sqlite3.connect(config.DATABASE_FILE) as conn:
        conn.execute("DROP TABLE episodes")
        conn.execute(
            """CREATE TABLE episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, base_name TEXT UNIQUE NOT NULL,
                speaker TEXT NOT NULL, title TEXT, description TEXT, tags TEXT,
                publish_date TEXT, category TEXT, kind TEXT NOT NULL, episode_url TEXT,
                simulated INTEGER NOT NULL DEFAULT 0, scheduled INTEGER NOT NULL DEFAULT 0,
                backdated INTEGER NOT NULL DEFAULT 0, email_sent INTEGER NOT NULL DEFAULT 0,
                audio_path TEXT, transcript_path TEXT, enrichment_path TEXT,
                sermon_seconds REAL NOT NULL, processing_seconds REAL NOT NULL, created_at TEXT NOT NULL)"""
        )
        for name, url, simulated in (
            ("a", "https://www.spreaker.com/episode/anna-titel--75397245", 0),
            ("b", "https://www.spreaker.com/episode/75250000", 0),
            ("c", "https://www.spreaker.com/simulated-episode/123", 1),
        ):
            conn.execute(
                "INSERT INTO episodes (base_name, speaker, kind, episode_url, simulated, sermon_seconds,"
                " processing_seconds, created_at) VALUES (?, 'A', 'manual', ?, ?, 1, 1, 'x')",
                (name, url, simulated),
            )

    db.init_db()

    rows = {row["base_name"]: row for row in episode_store.list_published()}
    assert (rows["a"]["provider"], rows["a"]["provider_episode_id"]) == ("spreaker", "75397245")
    assert rows["b"]["provider_episode_id"] == "75250000"
    assert rows["c"]["provider_episode_id"] is None


def test_list_merges_provider_and_history(client, tmp_env, monkeypatch):
    _configure_real_spreaker(monkeypatch)
    spreaker_episode_store.replace_all([
        {"episode_id": 500, "title": "På Spreaker", "description": "Text\nTalare: Bo", "published_at": "2026-09-01 10:00:00"},
    ])
    _record(tmp_env, "samma", provider_id=500)        # finns även hos Spreaker
    _record(tmp_env, "nyare", provider_id=600)        # publicerad efter senaste hämtningen
    _record(tmp_env, "simulerad", simulated=True)     # ska inte synas med riktigt konto

    data = client.get("/api/episodes").json()
    items = {item["id"]: item for item in data["items"]}

    assert set(items) == {"500", "600"}
    # Spreakers version vinner, men appen vet att den publicerade den och har transkriptet.
    assert items["500"]["title"] == "På Spreaker"
    assert items["500"]["in_history"] and items["500"]["on_provider"] and items["500"]["has_transcript"]
    # Ännu inte i den hämtade listan, men finns hos Spreaker - går att spara.
    assert items["600"]["editable"] is True and items["600"]["on_provider"] is False
    assert data["provider"]["capabilities"]["update_episode"] is True


def test_simulated_history_is_listed_but_not_editable(client, tmp_env, monkeypatch):
    """Med simulerad publicering visas appens historik - med Kopiera i stället för Spara."""
    monkeypatch.setattr(config, "SPREAKER_SIMULATE", True)
    _record(tmp_env, "prov", simulated=True)
    history_id = _history_id("prov")

    status = client.get("/api/episodes/status").json()
    assert status["visible"] is True

    item = client.get("/api/episodes").json()["items"][0]
    assert item["id"] == f"h{history_id}"
    assert item["simulated"] and not item["editable"] and item["has_transcript"]

    # Går inte att spara hos Spreaker - varken synligt eller via direktanrop.
    assert client.put(f"/api/episodes/h{history_id}", json={"title": "x"}).status_code == 403


def test_regenerate_history_item_uses_saved_transcript(client, tmp_env, monkeypatch):
    monkeypatch.setattr(config, "SPREAKER_SIMULATE", True)
    prompts = []
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt, temperature=None: prompts.append(prompt) or "Ny text")
    _record(tmp_env, "prov", simulated=True, transcript="Det sparade transkriptet.")
    history_id = _history_id("prov")

    res = client.post(f"/api/episodes/h{history_id}/regenerate", json={"regenerate_description": True})
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    deadline = time.time() + 15
    while time.time() < deadline:
        data = client.get(f"/api/process/status/{job_id}").json()
        if data["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.1)

    assert data["status"] == "done"
    assert data["result"]["id"] == f"h{history_id}"
    assert data["result"]["description"].endswith("Talare: Anna")
    assert "Det sparade transkriptet." in prompts[0]


def test_regenerate_history_item_without_transcript_is_refused(client, tmp_env, monkeypatch):
    monkeypatch.setattr(config, "SPREAKER_SIMULATE", True)
    _record(tmp_env, "borta", simulated=True, transcript=None)
    res = client.post(f"/api/episodes/h{_history_id('borta')}/regenerate", json={"regenerate_title": True})
    assert res.status_code == 403
    assert client.post("/api/episodes/h99999/regenerate", json={"regenerate_title": True}).status_code == 404


def test_published_episode_is_recorded_with_provider(client, stub_pipeline, tmp_path, monkeypatch):
    """Ett vanligt jobb sparar tjänst och avsnitts-id i historiken."""
    from modules import spreaker_client
    from tests.test_api_queue import _make_wav, _wait_until_finished

    monkeypatch.setattr(spreaker_client, "publish_episode", lambda **kwargs: {
        "episode_id": 4242, "episode_url": "https://www.spreaker.com/episode/anna--4242",
        "simulated": False, "scheduled": False, "backdated": False,
    })
    audio_path = tmp_path / "sermon.wav"
    _make_wav(audio_path, duration_seconds=1.0)
    with open(audio_path, "rb") as f:
        up = client.post("/api/upload", files={"file": ("sermon.wav", f, "audio/wav")}).json()
    client.post("/api/process", json={
        "file_id": up["file_id"], "start_seconds": 0, "end_seconds": up["duration_seconds"],
        "speaker": "Anna", "title": "", "description": "", "category": "", "publish_date": "",
    })
    _wait_until_finished(client)

    row = episode_store.list_published()[0]
    assert (row["provider"], row["provider_episode_id"]) == ("spreaker", "4242")
