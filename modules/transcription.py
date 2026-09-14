"""
Modul: transcription
Transkriberar ljud till text. Stöder två lägen:

1. OpenAI Whisper API (standard) - kräver OPENAI_API_KEY
2. Lokal Whisper-modell (USE_LOCAL_WHISPER=true) - körs helt offline,
   kräver att paketet "openai-whisper" eller "faster-whisper" är installerat.
"""
from pathlib import Path
import config

_local_model = None  # lazy-laddas bara vid behov


def transcribe_audio(audio_path: Path) -> str:
    """
    Transkriberar en ljudfil till text och returnerar hela transkriptet.
    """
    if config.USE_LOCAL_WHISPER:
        return _transcribe_local(audio_path)
    return _transcribe_openai(audio_path)


def _transcribe_openai(audio_path: Path) -> str:
    from openai import OpenAI

    if not config.OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY saknas i .env. Sätt en nyckel eller aktivera "
            "USE_LOCAL_WHISPER=true för lokal transkribering."
        )

    client = OpenAI(api_key=config.OPENAI_API_KEY)

    with open(audio_path, "rb") as f:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="sv",
        )
    return transcript.text


def _transcribe_local(audio_path: Path) -> str:
    global _local_model
    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError(
            "Paketet 'openai-whisper' är inte installerat. Kör: "
            "pip install openai-whisper"
        ) from exc

    if _local_model is None:
        _local_model = whisper.load_model(config.LOCAL_WHISPER_MODEL)

    result = _local_model.transcribe(str(audio_path), language="sv")
    return result["text"]
