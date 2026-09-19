"""
Delade pytest-fixturer. Pekar konfigurationen mot temporära kataloger och
en tom databas per test (via monkeypatch), så tester aldrig rör den
riktiga uploads/-, processed/-, bulk_import/- eller predikan.db-filen i
projektet.
"""
import pytest

import config
from modules import db


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Omdirigerar config till temporära kataloger/databas för ett enskilt test."""
    upload_dir = tmp_path / "uploads"
    processed_dir = tmp_path / "processed"
    bulk_dir = tmp_path / "bulk_import"
    for d in (upload_dir, processed_dir, bulk_dir):
        d.mkdir()

    monkeypatch.setattr(config, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(config, "PROCESSED_DIR", processed_dir)
    monkeypatch.setattr(config, "BULK_IMPORT_DIR", bulk_dir)
    monkeypatch.setattr(config, "DATABASE_FILE", tmp_path / "test.db")
    monkeypatch.setattr(config, "MAX_STORED_EPISODES", 0)
    monkeypatch.setattr(config, "SPREAKER_SIMULATE", True)

    db.init_db()

    return {"upload_dir": upload_dir, "processed_dir": processed_dir, "bulk_dir": bulk_dir}


@pytest.fixture
def client(tmp_env):
    """
    En TestClient mot hela appen, med databas/kataloger omdirigerade av
    tmp_env. Används SOM context manager (`with`) internt - det är det
    enda sättet FastAPI/Starlette faktiskt kör startup/shutdown-hookarna
    (app.py:_on_startup/_on_shutdown), som initierar databasen och
    startar/stoppar den enda kö-arbetartråden.
    """
    from fastapi.testclient import TestClient

    import app as app_module

    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def stub_pipeline(monkeypatch):
    """
    Stubbar ut transkriberingen och AI-berikningen så köobjekt kan
    bearbetas snabbt och deterministiskt i tester, utan riktig
    Whisper/Ollama/OpenAI. Spreaker-publicering är redan simulerad via
    SPREAKER_SIMULATE=True (se tmp_env).

    OBS: transkriberingen körs i en separat process (se
    modules/transcription_worker.py) som gör en egen, färsk import av
    modules.transcription - att monkeypatcha transcription.transcribe_audio
    i testprocessen påverkar alltså INTE den processen. Här stubbas
    transcription_worker.transcribe direkt istället (samma modul som
    services/pipeline.py faktiskt anropar), vilket kringgår subprocessen
    helt och är både snabbare och enklare för tester som inte specifikt
    testar avbrytning (se test_api_queue.py för det fallet, som istället
    stubbar via en långsam fejk-processfunktion).
    """
    from modules import ai_enrichment, transcription_worker

    monkeypatch.setattr(transcription_worker, "transcribe", lambda path, base_dir, cancel_event: "Test-transkript.")
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt: "Stub-svar")
