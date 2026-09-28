"""
Svenska tecken (å/ä/ö) ska överleva vägen från transkriberingsprocessen
tillbaka till huvudprocessen (modules/transcription_worker.py <->
modules/transcription_worker_process.py).

Tidigare skrev processen JSON med ensure_ascii=False till en omdirigerad
stdout, som på Windows använder cp1252 - huvudprocessen läste som UTF-8,
så varje å/ä/ö blev "�" i alla sparade transkript.

Testet kör den RIKTIGA bakgrundsprocessen men får den att misslyckas med
ett känt felmeddelande som innehåller "å" - Groq-läget utan nyckel, som
slår till innan något nätverksanrop görs - så det behövs varken Whisper
eller nätverk (bara ffmpeg, för en sekund ljud att dela i block).
"""
import subprocess
import threading

import pytest

import config
from modules import transcription_worker


def test_swedish_characters_survive_worker_roundtrip(tmp_path, monkeypatch):
    """
    Svenska tecken i bakgrundsprocessens svar (här ett felmeddelande med
    "är") kommer fram oskadade - förr blev de "�" på Windows.
    """
    # Skickas med förfrågan till bakgrundsprocessen (se
    # transcription_worker.SETTINGS_KEYS) och gäller före en ev. riktig .env.
    monkeypatch.setattr(config, "TRANSCRIPTION_PROVIDER", "groq")
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    monkeypatch.setattr(config, "LOCAL_FALLBACK", False)
    monkeypatch.setattr(transcription_worker, "_process", None)

    audio = tmp_path / "Förförelsen av Gud.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", str(audio)],
        check=True,
    )

    try:
        with pytest.raises(RuntimeError) as exc_info:
            transcription_worker.transcribe(audio, config.BASE_DIR, threading.Event())
    finally:
        if transcription_worker._process is not None:
            transcription_worker._kill_worker(transcription_worker._process)

    message = str(exc_info.value)
    assert "�" not in message
    assert "gratis nyckel på console.groq.com" in message
