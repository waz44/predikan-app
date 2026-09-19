"""
Integrationstester mot API:et för bearbetningskön: lyckad bearbetning
(manuell och CSV-bulkimport), avbrytning mitt i transkribering, samt
köhanteringen (prioritera/ta bort/rensa) inklusive dess skyddsräcken för
ett objekt som bearbetas just nu.

Transkriberingen och AI-berikningen stubbas (se conftest.py:stub_pipeline)
så testerna körs snabbt och deterministiskt utan riktig
Whisper/Ollama/OpenAI. Spreaker-publicering simuleras via
config.SPREAKER_SIMULATE (satt av tmp_env).
"""
import time
import wave

import pytest


def _make_wav(path, duration_seconds=1.0, framerate=8000):
    """Skriver en minimal, tyst WAV-fil - kräver inget ffmpeg för att SKAPAS
    (bara för att sedan klippas/normaliseras av appen, precis som riktiga
    ljudfiler - ffmpeg är redan ett krav för att appen ska fungera alls)."""
    n_frames = int(duration_seconds * framerate)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(framerate)
        f.writeframes(b"\x00\x00" * n_frames)


def _wait_until_finished(client, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        q = client.get("/api/queue").json()
        if q["items"] and all(it["status"] in ("done", "error", "cancelled") for it in q["items"]):
            return q
        time.sleep(0.2)
    raise TimeoutError("Kön blev inte klar i tid")


def test_manual_upload_and_process_happy_path(client, stub_pipeline, tmp_path):
    audio_path = tmp_path / "sermon.wav"
    _make_wav(audio_path, duration_seconds=1.0)

    with open(audio_path, "rb") as f:
        r = client.post("/api/upload", files={"file": ("sermon.wav", f, "audio/wav")})
    assert r.status_code == 200
    up = r.json()

    r = client.post("/api/process", json={
        "file_id": up["file_id"], "start_seconds": 0, "end_seconds": up["duration_seconds"],
        "speaker": "Anna", "title": "", "description": "", "category": "", "publish_date": "",
    })
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    q = _wait_until_finished(client)
    item = next(it for it in q["items"] if it["job_id"] == job_id)
    assert item["status"] == "done"
    assert item["result"]["episode_url"]
    assert "Talare: Anna" in item["result"]["final_description"]

    stats = client.get("/api/stats").json()
    assert stats["total_count"] == 1


def test_bulk_import_happy_and_missing_file(client, stub_pipeline, tmp_env):
    _make_wav(tmp_env["bulk_dir"] / "ok.mp3", duration_seconds=1.0)

    csv_bytes = (
        b"filnamn,talare,datum,klockslag\n"
        b"ok.mp3,Anna,2026-01-01,10:00\n"
        b"missing.mp3,Bertil,2026-01-01,11:00\n"
    )
    r = client.post("/api/bulk-import", files={"file": ("t.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200

    q = _wait_until_finished(client)
    statuses = {it["speaker"]: it["status"] for it in q["items"]}
    assert statuses["Anna"] == "done"
    assert statuses["Bertil"] == "error"

    # Den lyckade filen ska vara borttagen ur bulk_import/, men källan för
    # den misslyckade raden finns förstås aldrig (den existerade aldrig).
    assert not (tmp_env["bulk_dir"] / "ok.mp3").exists()


def test_bulk_import_rerun_after_partial_success(client, stub_pipeline, tmp_env):
    """En omkörning av samma CSV ska bara misslyckas för redan lyckade rader, inte blockera hela importen."""
    _make_wav(tmp_env["bulk_dir"] / "ok.mp3", duration_seconds=1.0)
    csv_bytes = b"filnamn,talare,datum,klockslag\nok.mp3,Anna,2026-01-01,10:00\n"

    client.post("/api/bulk-import", files={"file": ("t.csv", csv_bytes, "text/csv")})
    _wait_until_finished(client)

    # Kör samma CSV igen - filen är redan borttagen från förra körningen
    r = client.post("/api/bulk-import", files={"file": ("t.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200, "en omkörning ska accepteras av valideringen, inte blockeras direkt"

    q = _wait_until_finished(client)
    assert len(q["items"]) == 2
    assert q["items"][1]["status"] == "error"
    assert "hittades inte" in q["items"][1]["error"]


def _queue_slow_job(client, monkeypatch, tmp_path, speaker, filename="sermon.wav"):
    from modules import ai_enrichment, transcription_worker

    def _slow_transcribe(path, base_dir, cancel_event):
        for _ in range(50):  # 5s i 0.1s-steg
            if cancel_event.is_set():
                raise transcription_worker.TranscriptionCancelled("Avbruten.")
            time.sleep(0.1)
        return "Test-transkript."

    monkeypatch.setattr(transcription_worker, "transcribe", _slow_transcribe)
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt: "Stub-svar")

    audio_path = tmp_path / filename
    _make_wav(audio_path, duration_seconds=1.0)
    with open(audio_path, "rb") as f:
        r = client.post("/api/upload", files={"file": (filename, f, "audio/wav")})
    up = r.json()
    r = client.post("/api/process", json={
        "file_id": up["file_id"], "start_seconds": 0, "end_seconds": up["duration_seconds"],
        "speaker": speaker, "title": "", "description": "", "category": "", "publish_date": "",
    })
    return r.json()["job_id"]


def _wait_until_running(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = next(it for it in client.get("/api/queue").json()["items"] if it["job_id"] == job_id)
        if item["status"] == "running":
            return item
        time.sleep(0.1)
    raise TimeoutError("Jobbet startade aldrig")


def _cancel_and_wait(client, job_id, timeout=5.0):
    """Avbryter ett pågående jobb och väntar in att det verkligen nått ett sluttillstånd, så
    kö-arbetartråden inte fortfarande skriver till (test-)databasen när testet/fixturen städar upp."""
    client.post(f"/api/queue/cancel/{job_id}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = next(it for it in client.get("/api/queue").json()["items"] if it["job_id"] == job_id)
        if item["status"] in ("cancelled", "error", "done"):
            return
        time.sleep(0.1)


def test_eta_timer_missing_when_no_history(client, monkeypatch, tmp_path):
    """Utan tidigare bearbetningshistorik kan ingen tidsuppskattning göras - fältet ska vara None, inte ett påhittat värde."""
    job_id = _queue_slow_job(client, monkeypatch, tmp_path, "Utan historik")
    item = _wait_until_running(client, job_id)
    assert item["estimated_seconds"] is None
    assert item["estimated_completion_at"] is None
    _cancel_and_wait(client, job_id)


def test_eta_timer_present_once_history_exists(client, monkeypatch, tmp_path, tmp_env):
    """Med tidigare historik ska en beräknad sluttidpunkt i framtiden anges."""
    from datetime import UTC, datetime

    from modules import episode_store

    episode_store.record_episode({
        "base_name": "tidigare-ep",
        "speaker": "Talare",
        "title": "Titel",
        "description": "Beskrivning",
        "tags": [],
        "publish_date": "",
        "category": "",
        "kind": "manual",
        "episode_url": "https://example.com/tidigare-ep",
        "simulated": True,
        "scheduled": False,
        "backdated": False,
        "email_sent": False,
        "audio_path": str(tmp_env["processed_dir"] / "tidigare-ep-clipped.mp3"),
        "transcript_path": None,
        "enrichment_path": None,
        "sermon_seconds": 100.0,
        "processing_seconds": 50.0,
        "created_at": "2026-01-01T00:00:00",
    })

    job_id = _queue_slow_job(client, monkeypatch, tmp_path, "Med historik")
    item = _wait_until_running(client, job_id)
    assert item["estimated_seconds"] is not None

    completion_at = datetime.fromisoformat(item["estimated_completion_at"])
    if completion_at.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(UTC)
    assert completion_at > now
    _cancel_and_wait(client, job_id)


def test_cancel_during_transcription(client, monkeypatch, tmp_path):
    """Avbryter ett jobb medan det (simulerat) sitter i transkriberingssteget - ska bli 'cancelled' snabbt, inte vänta ut hela stubben."""
    from modules import ai_enrichment, transcription_worker

    def _slow_transcribe(path, base_dir, cancel_event):
        for _ in range(100):  # 10s i 0.1s-steg, avbryts långt innan
            if cancel_event.is_set():
                raise transcription_worker.TranscriptionCancelled("Transkriberingen avbröts av användaren.")
            time.sleep(0.1)
        return "hann aldrig klart"

    monkeypatch.setattr(transcription_worker, "transcribe", _slow_transcribe)
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt: "Stub")

    audio_path = tmp_path / "sermon.wav"
    _make_wav(audio_path, duration_seconds=1.0)
    with open(audio_path, "rb") as f:
        r = client.post("/api/upload", files={"file": ("sermon.wav", f, "audio/wav")})
    up = r.json()
    r = client.post("/api/process", json={
        "file_id": up["file_id"], "start_seconds": 0, "end_seconds": up["duration_seconds"],
        "speaker": "Cancel Test", "title": "", "description": "", "category": "", "publish_date": "",
    })
    job_id = r.json()["job_id"]

    deadline = time.time() + 10
    while time.time() < deadline:
        it = client.get("/api/queue").json()["items"][0]
        if it["status"] == "running":
            trans = next((s for s in it["steps"] if s["key"] == "transcription"), None)
            if trans and trans["status"] == "running":
                break
        time.sleep(0.1)
    else:
        pytest.fail("transkriberingssteget startade aldrig")

    t0 = time.time()
    r = client.post(f"/api/queue/cancel/{job_id}")
    assert r.json() == {"cancelled": True}

    deadline = time.time() + 5
    while time.time() < deadline:
        it = client.get("/api/queue").json()["items"][0]
        if it["status"] in ("cancelled", "error", "done"):
            break
        time.sleep(0.1)

    assert it["status"] == "cancelled"
    assert time.time() - t0 < 5, "avbrytningen ska vara nästan omedelbar, inte vänta ut hela den simulerade transkriberingen"


def test_queue_management_prioritize_remove_clear(client, tmp_env):
    """Testar prioritera/ta bort/rensa mot pausad kö med rena bulk-rader (aldrig bearbetade, snabbt att sätta upp)."""
    client.post("/api/queue/pause")

    csv_bytes = (
        b"filnamn,talare,datum,klockslag\n"
        b"a.mp3,Alpha,2026-01-01,10:00\n"
        b"b.mp3,Bravo,2026-01-01,11:00\n"
        b"c.mp3,Charlie,2026-01-01,12:00\n"
    )
    r = client.post("/api/bulk-import", files={"file": ("t.csv", csv_bytes, "text/csv")})
    items = r.json()["items"]

    q = client.get("/api/queue").json()
    assert q["paused"] is True
    assert [it["speaker"] for it in q["items"]] == ["Alpha", "Bravo", "Charlie"]

    # Prioritera Charlie till första platsen
    r = client.post(f"/api/queue/prioritize/{items[2]['queue_id']}")
    assert r.status_code == 200
    q = client.get("/api/queue").json()
    assert [it["speaker"] for it in q["items"]] == ["Charlie", "Alpha", "Bravo"]

    # Ta bort Bravo
    r = client.delete(f"/api/queue/{items[1]['queue_id']}")
    assert r.status_code == 200
    q = client.get("/api/queue").json()
    assert [it["speaker"] for it in q["items"]] == ["Charlie", "Alpha"]

    # Rensa allt
    r = client.post("/api/queue/clear")
    assert r.json() == {"removed": 2}
    assert client.get("/api/queue").json()["items"] == []


def test_cannot_remove_or_prioritize_a_running_item(client, monkeypatch, tmp_path):
    from modules import ai_enrichment, transcription_worker

    def _slow_transcribe(path, base_dir, cancel_event):
        for _ in range(50):
            if cancel_event.is_set():
                raise transcription_worker.TranscriptionCancelled("avbruten")
            time.sleep(0.1)
        return "text"

    monkeypatch.setattr(transcription_worker, "transcribe", _slow_transcribe)
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt: "Stub")

    audio_path = tmp_path / "sermon.wav"
    _make_wav(audio_path, duration_seconds=1.0)
    with open(audio_path, "rb") as f:
        r = client.post("/api/upload", files={"file": ("sermon.wav", f, "audio/wav")})
    up = r.json()
    r = client.post("/api/process", json={
        "file_id": up["file_id"], "start_seconds": 0, "end_seconds": up["duration_seconds"],
        "speaker": "Running Guard", "title": "", "description": "", "category": "", "publish_date": "",
    })
    job_id = r.json()["job_id"]
    queue_id = r.json()["queue_id"]

    deadline = time.time() + 10
    while time.time() < deadline:
        it = client.get("/api/queue").json()["items"][0]
        if it["status"] == "running":
            break
        time.sleep(0.1)
    else:
        pytest.fail("jobbet startade aldrig")

    r = client.delete(f"/api/queue/{queue_id}")
    assert r.status_code == 400

    r = client.post(f"/api/queue/prioritize/{queue_id}")
    assert r.status_code == 400

    # Städa: avbryt jobbet OCH vänta in att det verkligen landar som
    # 'cancelled' innan testet (och därmed client-fixturens nedstängning av
    # kö-arbetartråden) avslutas. Utan denna väntan kan bakgrundstråden
    # fortfarande sitta i _run_processing_job när nästa tests tmp_env redan
    # bytt ut config.DATABASE_FILE under fötterna på den.
    client.post(f"/api/queue/cancel/{job_id}")
    deadline = time.time() + 5
    while time.time() < deadline:
        it = client.get("/api/queue").json()["items"][0]
        if it["status"] in ("cancelled", "error", "done"):
            break
        time.sleep(0.1)
    assert it["status"] == "cancelled"
