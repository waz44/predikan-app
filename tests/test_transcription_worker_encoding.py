"""
Svenska tecken (å/ä/ö) ska överleva vägen från transkriberingsprocessen
tillbaka till huvudprocessen (modules/transcription_worker.py <->
modules/transcription_worker_process.py).

Tidigare skrev processen JSON med ensure_ascii=False till en omdirigerad
stdout, som på Windows använder cp1252 - huvudprocessen läste som UTF-8,
så varje å/ä/ö blev "�" i alla sparade transkript.

Testet kör den RIKTIGA bakgrundsprocessen men får den att misslyckas med
ett känt felmeddelande som innehåller "ä" - OpenAI-lägets storlekskontroll
(> 25 MB), som slår till innan något nätverksanrop görs - så det behövs
varken Whisper, riktigt ljud eller nätverk.
"""
import threading

import pytest

import config
from modules import transcription_worker


def test_swedish_characters_survive_worker_roundtrip(tmp_path, monkeypatch):
    # Skickas med förfrågan till bakgrundsprocessen (se
    # transcription_worker.SETTINGS_KEYS) och gäller före en ev. riktig .env.
    monkeypatch.setattr(config, "USE_LOCAL_WHISPER", False)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-nyckel-anvands-aldrig")
    monkeypatch.setattr(transcription_worker, "_process", None)

    audio = tmp_path / "Förförelsen av Gud.mp3"
    with open(audio, "wb") as f:
        f.truncate(26 * 1024 * 1024)  # över OpenAI:s gräns på 25 MB

    try:
        with pytest.raises(RuntimeError) as exc_info:
            transcription_worker.transcribe(audio, config.BASE_DIR, threading.Event())
    finally:
        if transcription_worker._process is not None:
            transcription_worker._kill_worker(transcription_worker._process)

    message = str(exc_info.value)
    assert "�" not in message
    assert "Ljudfilen är 26.0 MB" in message
