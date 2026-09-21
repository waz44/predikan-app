"""
Modul: transcription
Transkriberar ljud till text. Stöder två lägen:

1. OpenAI Whisper API (standard) - kräver OPENAI_API_KEY
2. Lokal Whisper-modell (USE_LOCAL_WHISPER=true) - körs helt offline.
   Stöder BÅDA "faster-whisper" och "openai-whisper" (se requirements.txt
   - kommentera in/ur vilket du vill använda). Vilket paket som faktiskt
   är installerat upptäcks automatiskt (se _load_local_model) - "faster-
   whisper" provas först eftersom det är snabbare och bättre underhållet,
   annars faller den tillbaka till "openai-whisper".
"""
import sys
from pathlib import Path

import config

_local_model = None  # lazy-laddas bara vid behov
_local_backend = None  # "faster-whisper" | "openai-whisper", satt samtidigt som _local_model

# OpenAI Whisper API avvisar filer större än 25 MB. En klippt predikan i mp3
# @192k passerar gränsen redan vid ~17 min, så längre predikningar failar
# annars med ett kryptiskt API-fel. Lokal Whisper har ingen sådan gräns.
_OPENAI_WHISPER_MAX_BYTES = 25 * 1024 * 1024


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

    size_bytes = audio_path.stat().st_size
    if size_bytes > _OPENAI_WHISPER_MAX_BYTES:
        raise RuntimeError(
            f"Ljudfilen är {size_bytes / 1024 / 1024:.1f} MB, men OpenAI Whisper API "
            "tar emot högst 25 MB. Korta ner klippet, eller sätt USE_LOCAL_WHISPER=true "
            "i .env för lokal transkribering utan storleksgräns."
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
    global _local_model, _local_backend
    if _local_model is None:
        _local_backend, _local_model = _load_local_model()

    if _local_backend == "faster-whisper":
        segments, _info = _local_model.transcribe(str(audio_path), language="sv")
        return "".join(segment.text for segment in segments)

    result = _local_model.transcribe(str(audio_path), language="sv")
    return result["text"]


def _load_local_model():
    """
    Laddar en lokal Whisper-modell med vilket bibliotek som råkar vara
    installerat - "faster-whisper" provas först (snabbare, bättre
    underhållet), annars "openai-whisper". Returnerar (backend, modell).
    """
    device = _resolve_device()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        pass
    else:
        _log_loading("faster-whisper", device)
        return "faster-whisper", WhisperModel(config.LOCAL_WHISPER_MODEL, device=device)

    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError(
            "Varken 'faster-whisper' eller 'openai-whisper' är installerat. "
            "Kör: pip install faster-whisper  (rekommenderas)  eller  "
            "pip install openai-whisper"
        ) from exc

    _log_loading("openai-whisper", device)
    return "openai-whisper", whisper.load_model(config.LOCAL_WHISPER_MODEL, device=device)


def _log_loading(backend: str, device: str) -> None:
    # OBS: medvetet stderr, inte stdout - transcription_worker_process.py
    # kör transkriberingen i en egen process och pratar med huvud-
    # processen över stdout med ett strikt en-JSON-rad-per-svar-protokoll
    # (se den modulens docstring). En utskrift på stdout här skulle bryta
    # det protokollet.
    print(
        f"[transcription] Laddar Whisper-modellen ({backend}) '{config.LOCAL_WHISPER_MODEL}' på enhet: {device}",
        file=sys.stderr,
    )


def _resolve_device() -> str:
    """
    Avgör vilken enhet (GPU/CPU) Whisper ska köra på - används av både
    faster-whisper och openai-whisper (se _load_local_model), som båda
    accepterar samma "cuda"/"cpu"-värden.

    Styrs av config.WHISPER_DEVICE ("auto" = default, annars "cuda" eller
    "cpu" för att tvinga ett val). "auto" försöker använda en NVIDIA-GPU via
    CUDA om PyTorch upptäcker en, annars faller den tillbaka till CPU.

    OBS: NVIDIA/CUDA är den enda GPU-acceleration som stöds av något av
    biblioteken. AMD- och Intel-GPU:er körs alltid på CPU oavsett
    inställning här. Auto-detekteringen förutsätter dessutom att PyTorch
    (`torch`) finns installerat (följer alltid med openai-whisper, men inte
    nödvändigtvis med en fristående faster-whisper-installation utan CUDA-
    stöd) - saknas det faller den tillbaka till CPU. Sätt WHISPER_DEVICE=cuda
    uttryckligen i .env om du vet att du har en NVIDIA-GPU men saknar torch.
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
