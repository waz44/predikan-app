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
        device = _resolve_device()
        print(f"[transcription] Laddar Whisper-modellen '{config.LOCAL_WHISPER_MODEL}' på enhet: {device}")
        _local_model = whisper.load_model(config.LOCAL_WHISPER_MODEL, device=device)

    result = _local_model.transcribe(str(audio_path), language="sv")
    return result["text"]


def _resolve_device() -> str:
    """
    Avgör vilken enhet (GPU/CPU) Whisper ska köra på.

    Styrs av config.WHISPER_DEVICE ("auto" = default, annars "cuda" eller
    "cpu" för att tvinga ett val). "auto" försöker använda en NVIDIA-GPU via
    CUDA om PyTorch upptäcker en, annars faller den tillbaka till CPU.

    OBS: openai-whisper (via PyTorch) stöder GPU-acceleration endast för
    NVIDIA-kort med CUDA. AMD- och Intel-GPU:er stöds inte på detta sätt,
    och kommer alltid köras på CPU oavsett inställning här.
    """
    if config.WHISPER_DEVICE in ("cuda", "cpu"):
        return config.WHISPER_DEVICE

    try:
        import torch
    except ImportError:
        return "cpu"

    return "cuda" if torch.cuda.is_available() else "cpu"


def save_transcript(transcript: str, transcript_path: Path) -> Path:
    """
    Sparar transkriptionen till en textfil.
    
    Args:
        transcript: Transkript-texten
        transcript_path: Sökväg där textfilen ska sparas
    
    Returns:
        Sökvägen till den sparade textfilen.
    """
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(transcript)
    return transcript_path
