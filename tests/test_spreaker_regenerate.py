"""
Tester för "Generera om"-funktionen i Hantera Spreaker-fliken
(routers/spreaker_episodes.py:regenerate_episode +
services/pipeline.py:_run_regenerate_job). Skriver ALDRIG till Spreaker
själv - bara till job["result"], som frontend sedan använder för att
fylla i redigeringsfälten (se static/app.js:pollRegenerateJob).
"""
import time

import config
from modules import spreaker_client, spreaker_episode_store, transcription_worker


def _configure_real_spreaker(monkeypatch):
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "tok")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "123")
    monkeypatch.setattr(config, "SPREAKER_SIMULATE", False)


def _stub_download(monkeypatch, calls):
    def fake_download(episode_id, dest_path):
        calls.append(episode_id)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"fake-audio")

    monkeypatch.setattr(spreaker_client, "download_episode_audio", fake_download)


def _wait_for_job(client, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = client.get(f"/api/process/status/{job_id}")
        data = res.json()
        if data["status"] in ("done", "error", "cancelled"):
            return data
        time.sleep(0.1)
    raise TimeoutError("Regenerera-jobbet blev inte klart i tid")


def test_regenerate_requires_configured(client, tmp_env, monkeypatch):
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "")
    res = client.post("/api/spreaker/episodes/1/regenerate", json={"regenerate_title": True})
    assert res.status_code == 403


def test_regenerate_requires_at_least_one_field(client, tmp_env, monkeypatch):
    _configure_real_spreaker(monkeypatch)
    res = client.post("/api/spreaker/episodes/1/regenerate", json={})
    assert res.status_code == 400


def test_regenerate_creates_queue_item(client, tmp_env, monkeypatch):
    _configure_real_spreaker(monkeypatch)
    spreaker_episode_store.replace_all([
        {"episode_id": 55, "title": "Gammal titel", "description": "Text\nTalare: Anna",
         "duration": 60000, "published_at": "2026-01-01 00:00:00", "site_url": "https://x/55", "plays_count": 0},
    ])

    res = client.post("/api/spreaker/episodes/55/regenerate", json={"regenerate_title": True})
    assert res.status_code == 200
    data = res.json()
    assert "job_id" in data and "queue_id" in data

    q = client.get("/api/queue").json()
    item = next(it for it in q["items"] if it["job_id"] == data["job_id"])
    assert item["kind"] == "regenerate"
    assert item["filename"] == "Gammal titel"
    assert item["speaker"] == "Anna"


def test_regenerate_happy_path_fills_result_and_never_updates_spreaker(client, tmp_env, monkeypatch):
    _configure_real_spreaker(monkeypatch)
    download_calls = []
    _stub_download(monkeypatch, download_calls)
    monkeypatch.setattr(transcription_worker, "transcribe", lambda path, base_dir, cancel_event: "Test-transkript.")
    from modules import ai_enrichment
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt: "Nytt AI-förslag")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("update_episode ska ALDRIG anropas av regenerering - bara fylla i result.")
    monkeypatch.setattr(spreaker_client, "update_episode", _fail_if_called)

    res = client.post("/api/spreaker/episodes/999/regenerate", json={"regenerate_title": True, "regenerate_description": True})
    job_id = res.json()["job_id"]

    result = _wait_for_job(client, job_id)
    assert result["status"] == "done"
    assert result["result"]["episode_id"] == 999
    assert result["result"]["title"] == "Nytt AI-förslag"
    assert result["result"]["description"] == "Nytt AI-förslag"
    assert download_calls == [999]


def test_regenerate_appends_talare_line_to_new_description(client, tmp_env, monkeypatch):
    """
    Samma konvention som _run_processing_job använder för NYA avsnitt:
    talaren som en egen rad sist i beskrivningen - annars tappar man
    Talare-kolumnen i Hantera Spreaker-tabellen (som läser ut den ur just
    den raden) så fort ett regenererat förslag sparas.
    """
    _configure_real_spreaker(monkeypatch)
    spreaker_episode_store.replace_all([
        {"episode_id": 321, "title": "Gammal titel", "description": "Gammal text\nTalare: Bertil",
         "duration": 60000, "published_at": "2026-01-01 00:00:00", "site_url": "https://x/321", "plays_count": 0},
    ])
    _stub_download(monkeypatch, [])
    monkeypatch.setattr(transcription_worker, "transcribe", lambda path, base_dir, cancel_event: "Test-transkript.")
    from modules import ai_enrichment
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt: "Ett nytt förslag om predikan.")

    res = client.post("/api/spreaker/episodes/321/regenerate", json={"regenerate_description": True})
    result = _wait_for_job(client, res.json()["job_id"])

    assert result["status"] == "done"
    assert result["result"]["description"] == "Ett nytt förslag om predikan.\n\nTalare: Bertil"


def test_regenerate_reuses_cached_transcript(client, tmp_env, monkeypatch):
    _configure_real_spreaker(monkeypatch)
    download_calls = []
    transcribe_calls = []
    _stub_download(monkeypatch, download_calls)

    def fake_transcribe(path, base_dir, cancel_event):
        transcribe_calls.append(path)
        return "Test-transkript."

    monkeypatch.setattr(transcription_worker, "transcribe", fake_transcribe)
    from modules import ai_enrichment
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt: "Förslag")

    # Första gången: inget cachat transkript - ska ladda ner + transkribera.
    res1 = client.post("/api/spreaker/episodes/42/regenerate", json={"regenerate_title": True})
    result1 = _wait_for_job(client, res1.json()["job_id"])
    assert result1["status"] == "done"
    assert download_calls == [42]
    assert len(transcribe_calls) == 1

    # Andra gången, utan force_retranscribe: ska återanvända det cachade transkriptet.
    res2 = client.post("/api/spreaker/episodes/42/regenerate", json={"regenerate_title": True})
    result2 = _wait_for_job(client, res2.json()["job_id"])
    assert result2["status"] == "done"
    assert download_calls == [42], "ska INTE ladda ner igen när ett transkript redan är cachat"
    assert len(transcribe_calls) == 1, "ska INTE transkribera igen när ett transkript redan är cachat"


def test_regenerate_force_retranscribe_ignores_cache(client, tmp_env, monkeypatch):
    _configure_real_spreaker(monkeypatch)
    download_calls = []
    _stub_download(monkeypatch, download_calls)
    monkeypatch.setattr(transcription_worker, "transcribe", lambda path, base_dir, cancel_event: "Test-transkript.")
    from modules import ai_enrichment
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt: "Förslag")

    res1 = client.post("/api/spreaker/episodes/7/regenerate", json={"regenerate_title": True})
    _wait_for_job(client, res1.json()["job_id"])
    assert download_calls == [7]

    res2 = client.post("/api/spreaker/episodes/7/regenerate", json={"regenerate_title": True, "force_retranscribe": True})
    result2 = _wait_for_job(client, res2.json()["job_id"])
    assert result2["status"] == "done"
    assert download_calls == [7, 7], "force_retranscribe ska ladda ner på nytt trots cachat transkript"
