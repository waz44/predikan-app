"""
Modul: transcription
Transkriberar ljud till text. Stöder tre lägen:

1. OpenAI Whisper API - kräver OPENAI_API_KEY
2. Lokal Whisper-modell (USE_LOCAL_WHISPER=true, LOCAL_ASR_ENGINE=whisper)
   - körs helt offline. Stöder BÅDA "faster-whisper" och "openai-whisper"
   (se requirements.txt). Vilket paket som faktiskt är installerat upptäcks
   automatiskt (se _load_local_model) - "faster-whisper" provas först.
   Med faster-whisper kan LOCAL_WHISPER_MODEL också vara en svensk
   KB-Whisper från Kungliga biblioteket, t.ex. "KBLab/kb-whisper-small",
   som gör ungefär fyra gånger färre fel på svenska än OpenAI:s modeller.
3. Pianissimo (USE_LOCAL_WHISPER=true, LOCAL_ASR_ENGINE=pianissimo) -
   Klangs svenska taligenkänning, körs offline via ONNX (paketet onnx-asr).
   Modellen klarar bara ca 20-30 sekunder åt gången, så ljudet delas upp
   vid pauser med en röstdetektor (VAD) och bitarnas text sätts ihop.

Modellen laddas bara om när inställningen ändras (se _loaded_key).
"""
import sys
from pathlib import Path

import config

_local_model = None  # lazy-laddas bara vid behov
_local_backend = None  # "faster-whisper" | "openai-whisper" | "pianissimo", satt samtidigt som _local_model
_loaded_key: tuple | None = None  # vilka inställningar _local_model laddades med

# Pianissimo läses in i bitar om högst så här många sekunder (modellens
# gräns är ca 20-30 s). Bitarna delas vid pauser av röstdetektorn.
_PIANISSIMO_MAX_SEGMENT_S = 20.0
_PIANISSIMO_SAMPLE_RATE = 16_000
_PIANISSIMO_FILES = ["encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "vocab.txt", "config.json"]

# OpenAI Whisper API avvisar filer större än 25 MB. En klippt predikan i mp3
# @192k passerar gränsen redan vid ~17 min, så längre predikningar failar
# annars med ett kryptiskt API-fel. Lokal Whisper har ingen sådan gräns.
_OPENAI_WHISPER_MAX_BYTES = 25 * 1024 * 1024


def transcribe_audio(audio_path: Path) -> str:
    """
    Transkriberar en ljudfil till text och returnerar hela transkriptet.

    Väljer motor utifrån inställningarna: OpenAI:s molntjänst, lokal Whisper
    (inklusive KB-Whisper) eller Pianissimo. Körs normalt i bakgrunds-
    processen (se transcription_worker_process.py), inte i webbservern.

    Args:
        audio_path: Ljudfilen som ska transkriberas.

    Returns:
        Hela transkriptet som en sammanhängande text.
    """
    if config.USE_LOCAL_WHISPER:
        if config.LOCAL_ASR_ENGINE == "pianissimo":
            return _transcribe_pianissimo(audio_path)
        return _transcribe_local(audio_path)
    return _transcribe_openai(audio_path)


def _ensure_model(key: tuple, loader) -> None:
    """
    Laddar (om) den lokala modellen när inställningarna ändrats sen sist.

    Args:
        key: Det som avgör vilken modell som behövs, t.ex.
            ("whisper", "KBLab/kb-whisper-small", "auto"). En annan nyckel än
            förra gången gör att modellen laddas om.
        loader: Funktion som laddar modellen och returnerar (motor, modell).
    """
    global _local_model, _local_backend, _loaded_key
    if _local_model is None or _loaded_key != key:
        _local_model = None  # släpp en ev. gammal modell innan en ny laddas
        _local_backend, _local_model = loader()
        _loaded_key = key


def _transcribe_openai(audio_path: Path) -> str:
    """
    Transkriberar via OpenAI:s Whisper-tjänst (whisper-1) i molnet.

    Args:
        audio_path: Ljudfilen (högst 25 MB).

    Returns:
        Transkriptet som text.

    Raises:
        RuntimeError: Om API-nyckel saknas eller filen är för stor.
    """
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
    """
    Transkriberar med en lokal Whisper-modell (faster-whisper eller openai-whisper).

    Args:
        audio_path: Ljudfilen, i valfritt format som ffmpeg kan läsa.

    Returns:
        Transkriptet som text.
    """
    _ensure_model(("whisper", config.LOCAL_WHISPER_MODEL, config.WHISPER_DEVICE), _load_local_model)

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

    Returns:
        (motor, modell) där motor är "faster-whisper" eller "openai-whisper".

    Raises:
        RuntimeError: Om inget Whisper-paket är installerat, eller om en
            KB-Whisper-modell valts utan faster-whisper.
    """
    device = _resolve_device()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        pass
    else:
        _log_loading("faster-whisper", device)
        # int8 på processor är flera gånger snabbare än full precision, med
        # i praktiken samma resultat. På GPU väljer faster-whisper själv.
        compute_type = "int8" if device == "cpu" else "default"
        return "faster-whisper", WhisperModel(config.LOCAL_WHISPER_MODEL, device=device, compute_type=compute_type)

    if "/" in config.LOCAL_WHISPER_MODEL:
        # Ett Hugging Face-namn som "KBLab/kb-whisper-small" - bara
        # faster-whisper kan läsa sådana modeller, inte openai-whisper.
        raise RuntimeError(
            f"Modellen '{config.LOCAL_WHISPER_MODEL}' kräver faster-whisper. "
            "Kör: pip install faster-whisper  - eller välj en vanlig Whisper-modell "
            "(t.ex. small) under Inställningar."
        )

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


def _transcribe_pianissimo(audio_path: Path) -> str:
    """
    Transkriberar med Pianissimo: ljudet delas vid pauser och bitarnas text sätts ihop.

    Args:
        audio_path: Ljudfilen, i valfritt format som ffmpeg kan läsa.

    Returns:
        Transkriptet som text, med ett mellanslag mellan bitarna.
    """
    _ensure_model(("pianissimo", config.PIANISSIMO_MODEL), _load_pianissimo)
    samples = _read_audio_16k_mono(audio_path)
    segments = _local_model.recognize(samples, sample_rate=_PIANISSIMO_SAMPLE_RATE)
    texts = [segment.text.strip() for segment in segments if segment.text and segment.text.strip()]
    return " ".join(texts)


def _read_audio_16k_mono(audio_path: Path):
    """
    Läser valfritt ljudformat (via pydub/ffmpeg, som resten av appen) som
    16 kHz mono float32 - det format Pianissimo tränats på.

    Args:
        audio_path: Ljudfilen.

    Returns:
        En numpy-array med värden mellan -1 och 1, 16 000 värden per sekund.
    """
    import numpy as np
    from pydub import AudioSegment

    audio = (
        AudioSegment.from_file(str(audio_path))
        .set_channels(1)
        .set_frame_rate(_PIANISSIMO_SAMPLE_RATE)
        .set_sample_width(2)
    )
    return np.frombuffer(audio.raw_data, dtype=np.int16).astype(np.float32) / 32768.0


def _load_pianissimo():
    """
    Laddar Pianissimo (ONNX, int8) + röstdetektorn Silero VAD via onnx-asr.
    PIANISSIMO_MODEL är ett Hugging Face-förråd (laddas ner första gången,
    ca 920 MB, och cachas sedan) eller en lokal mapp med samma filer.

    Returns:
        ("pianissimo", modell) där modellen redan är kopplad till röstdetektorn.

    Raises:
        RuntimeError: Om paketet onnx-asr inte är installerat.
    """
    try:
        import onnx_asr
    except ImportError as exc:
        raise RuntimeError(
            'Pianissimo kräver paketet onnx-asr. Kör: pip install "onnx-asr[cpu,hub]"'
        ) from exc

    model_path = Path(config.PIANISSIMO_MODEL)
    if not model_path.is_dir():
        model_path = _download_pianissimo(config.PIANISSIMO_MODEL)

    print(f"[transcription] Laddar Pianissimo från {model_path}", file=sys.stderr)
    model = onnx_asr.load_model("nemo-conformer-tdt", model_path, quantization="int8")
    vad = onnx_asr.load_vad("silero")
    return "pianissimo", model.with_vad(vad, max_speech_duration_s=_PIANISSIMO_MAX_SEGMENT_S)


def _download_pianissimo(repo_id: str) -> Path:
    """
    Hämtar Pianissimos filer till appens egen mapp (models/<förråd>) - en
    gång; finns filerna redan används de direkt, utan kontakt med Hugging
    Face. Egen mapp i stället för den delade Hugging Face-cachen, eftersom
    config.json behöver kompletteras (se _fix_pianissimo_config).

    Args:
        repo_id: Hugging Face-förrådet, t.ex. "moonhouse/pianissimo-sv-onnx".

    Returns:
        Mappen med modellfilerna.
    """
    target = config.BASE_DIR / "models" / repo_id.rsplit("/", 1)[-1]
    if not all((target / name).exists() for name in _PIANISSIMO_FILES):
        from huggingface_hub import snapshot_download

        print(f"[transcription] Hämtar Pianissimo ({repo_id}, ca 920 MB) till {target}...", file=sys.stderr)
        snapshot_download(repo_id, allow_patterns=_PIANISSIMO_FILES, local_dir=target)
    _fix_pianissimo_config(target / "config.json")
    return target


def _fix_pianissimo_config(config_path: Path) -> None:
    """
    Pianissimo (liksom Parakeet v3) använder 128 frekvensband. ONNX-versionens
    config.json anger det som "features", men onnx-asr läser "features_size"
    och antar annars 80 - då vägrar modellen ta emot ljudet ("Got: 80
    Expected: 128"). Lägger till nyckeln om den saknas.

    Args:
        config_path: Modellens config.json.
    """
    import json

    data = json.loads(config_path.read_text(encoding="utf-8"))
    if "features_size" not in data and "features" in data:
        data["features_size"] = data["features"]
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _log_loading(backend: str, device: str) -> None:
    """
    Skriver en rad om vilken modell som laddas, på vilken enhet.

    Args:
        backend: "faster-whisper" eller "openai-whisper".
        device: "cuda" (grafikkort) eller "cpu".
    """
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
