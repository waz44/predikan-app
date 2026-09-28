"""
Modul: transcription
Transkriberar ljud till text. Var det görs styrs av TRANSCRIPTION_PROVIDER:

1. "groq" - Groqs Whisper large-v3 på nätet (gratisnivå), kräver GROQ_API_KEY
2. "openai" - OpenAI:s Whisper API (kostar), kräver OPENAI_API_KEY
3. "local" - på den egna datorn, helt offline, med LOCAL_ASR_ENGINE:
   - "whisper": en lokal Whisper-modell. Stöder BÅDE "faster-whisper" och
     "openai-whisper" (se requirements.txt) - vilket paket som är
     installerat upptäcks automatiskt (se _load_local_model), och
     "faster-whisper" provas först. Med faster-whisper kan
     LOCAL_WHISPER_MODEL också vara en svensk KB-Whisper från Kungliga
     biblioteket, t.ex. "KBLab/kb-whisper-small", som gör ungefär fyra
     gånger färre fel på svenska än OpenAI:s modeller.
   - "pianissimo": Klangs svenska taligenkänning, körs offline via ONNX
     (paketet onnx-asr). Modellen klarar bara ca 20-30 sekunder åt gången,
     så ljudet delas dessutom upp vid pauser med en röstdetektor (VAD).

Ljudet transkriberas i block om ca 10 minuter (delade i pauser i talet, se
audio_processor.split_for_transcription) i stället för hela filen i ett
stycke: blocken ryms inom tjänsternas storleksgräns (25 MB), ett fel eller
en väntan på gratiskvoten gäller bara ett block, och framstegen rapporteras
block för block. Slutet av föregående blocks text skickas med som
sammanhang, så att namn och stavning håller i sig över blockgränserna.

Med LOCAL_FALLBACK=true görs ett block som tjänsten på nätet inte klarar
(t.ex. slut på gratiskvoten) med den lokala motorn i stället.

Modellen laddas bara om när inställningen ändras (se _loaded_key).
"""
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import config
from modules import audio_processor

_local_model = None  # lazy-laddas bara vid behov
_local_backend = None  # "faster-whisper" | "openai-whisper" | "pianissimo", satt samtidigt som _local_model
_loaded_key: tuple | None = None  # vilka inställningar _local_model laddades med

# Pianissimo läses in i bitar om högst så här många sekunder (modellens
# gräns är ca 20-30 s). Bitarna delas vid pauser av röstdetektorn.
_PIANISSIMO_MAX_SEGMENT_S = 20.0
_PIANISSIMO_SAMPLE_RATE = 16_000
_PIANISSIMO_FILES = ["encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "vocab.txt", "config.json"]

# Groq och OpenAI avvisar filer större än 25 MB. Blocken (se
# audio_processor.split_for_transcription) är ca 3,6 MB, så gränsen nås bara
# om något gått snett - då hellre ett tydligt fel än ett kryptiskt API-fel.
_API_MAX_BYTES = 25 * 1024 * 1024
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Så här många tecken av föregående blocks text skickas med som sammanhang
# (tjänsterna tar högst 224 tokens).
_CONTEXT_CHARS = 200

# Vid "för många anrop" (429, t.ex. Groqs gräns på 2 timmar ljud per timme)
# väntar appen den tid tjänsten anger - högst så här länge per gång och så
# här många gånger - innan blocket räknas som misslyckat.
_RATE_LIMIT_MAX_WAIT_S = 15 * 60
_RATE_LIMIT_ATTEMPTS = 6

# Anropas med (klara block, antal block) efter varje block.
ProgressCallback = Callable[[int, int], None]


def transcribe_audio(audio_path: Path, on_progress: ProgressCallback | None = None) -> str:
    """
    Transkriberar en ljudfil till text, block för block, och returnerar hela transkriptet.

    Körs normalt i bakgrundsprocessen (se transcription_worker_process.py),
    inte i webbservern.

    Args:
        audio_path: Ljudfilen som ska transkriberas.
        on_progress: Anropas med (klara block, antal block) efter varje block.

    Returns:
        Hela transkriptet som en sammanhängande text.
    """
    provider = config.TRANSCRIPTION_PROVIDER
    # Lokalt: okomprimerad wav (ingen kvalitetsförlust). På nätet: liten mp3.
    codec = "wav" if provider == "local" else "mp3"
    with tempfile.TemporaryDirectory(prefix="predikan-block-") as tmp:
        blocks = audio_processor.split_for_transcription(audio_path, Path(tmp), codec=codec)
        texts: list[str] = []
        context = ""
        for index, block in enumerate(blocks, start=1):
            text = _transcribe_block_with_fallback(provider, block, context).strip()
            if text:
                texts.append(text)
                context = text[-_CONTEXT_CHARS:]
            if on_progress:
                on_progress(index, len(blocks))
    return " ".join(texts)


def _transcribe_block_with_fallback(provider: str, block: Path, context: str) -> str:
    """
    Transkriberar ett block - och med LOCAL_FALLBACK lokalt om tjänsten på nätet misslyckas.

    Args:
        provider: "local", "groq" eller "openai".
        block: Blockets ljudfil.
        context: Slutet av föregående blocks text.

    Returns:
        Blockets text.
    """
    try:
        return _transcribe_block(provider, block, context)
    except Exception as exc:
        if provider == "local" or not config.LOCAL_FALLBACK:
            raise
        print(f"[transcription] {provider} misslyckades ({exc}) - transkriberar blocket lokalt.", file=sys.stderr)
        return _transcribe_block("local", block, context)


def _transcribe_block(provider: str, block: Path, context: str) -> str:
    """Transkriberar ett block med den angivna tjänsten eller den lokala motorn."""
    if provider == "local":
        if config.LOCAL_ASR_ENGINE == "pianissimo":
            return _transcribe_pianissimo(block)
        return _transcribe_local(block, context)
    if provider == "groq":
        if not config.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY saknas. Skapa en gratis nyckel på console.groq.com och fyll i den under Inställningar.")
        return _transcribe_api(block, config.GROQ_API_KEY, config.GROQ_TRANSCRIPTION_MODEL, context, _GROQ_BASE_URL)
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY saknas. Fyll i en nyckel eller välj en annan transkribering under Inställningar.")
    return _transcribe_api(block, config.OPENAI_API_KEY, "whisper-1", context)


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


def _retry_after_seconds(exc) -> float:
    """Hur länge tjänsten ber oss vänta efter "för många anrop" (retry-after), annars 60 s."""
    try:
        return float(exc.response.headers.get("retry-after", 60))
    except Exception:
        return 60.0


def _transcribe_api(block: Path, api_key: str, model: str, context: str, base_url: str | None = None) -> str:
    """
    Transkriberar ett block via en Whisper-tjänst med OpenAI:s gränssnitt (OpenAI eller Groq).

    Vid "för många anrop" (429) väntas den tid tjänsten anger och blocket
    skickas igen - Groqs gratisnivå tar t.ex. högst 2 timmar ljud per timme.

    Args:
        block: Blockets ljudfil (högst 25 MB).
        api_key: Tjänstens nyckel.
        model: T.ex. "whisper-large-v3" (Groq) eller "whisper-1" (OpenAI).
        context: Slutet av föregående blocks text, som sammanhang.
        base_url: Tjänstens adress (None = OpenAI).

    Returns:
        Blockets text.

    Raises:
        RuntimeError: Om blocket är för stort.
        openai.APIError: Om tjänsten svarar med fel även efter väntan.
    """
    from openai import OpenAI, RateLimitError

    size_bytes = block.stat().st_size
    if size_bytes > _API_MAX_BYTES:
        raise RuntimeError(f"Ljudblocket är {size_bytes / 1024 / 1024:.1f} MB - tjänsten tar emot högst 25 MB.")

    client = OpenAI(api_key=api_key, base_url=base_url)
    extra = {"prompt": context} if context else {}
    for attempt in range(1, _RATE_LIMIT_ATTEMPTS + 1):
        try:
            with open(block, "rb") as f:
                result = client.audio.transcriptions.create(
                    model=model, file=f, language="sv", temperature=0, **extra,
                )
            return result.text
        except RateLimitError as exc:
            if attempt == _RATE_LIMIT_ATTEMPTS:
                raise
            wait = min(_retry_after_seconds(exc), _RATE_LIMIT_MAX_WAIT_S)
            print(f"[transcription] Tjänstens gräns nådd - väntar {wait:.0f} s ({attempt}/{_RATE_LIMIT_ATTEMPTS}).", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _transcribe_local(audio_path: Path, context: str = "") -> str:
    """
    Transkriberar med en lokal Whisper-modell (faster-whisper eller openai-whisper).

    Args:
        audio_path: Ljudfilen, i valfritt format som ffmpeg kan läsa.
        context: Slutet av föregående blocks text, som sammanhang.

    Returns:
        Transkriptet som text.
    """
    _ensure_model(("whisper", config.LOCAL_WHISPER_MODEL, config.WHISPER_DEVICE), _load_local_model)
    prompt = context or None

    if _local_backend == "faster-whisper":
        segments, _info = _local_model.transcribe(str(audio_path), language="sv", initial_prompt=prompt)
        return "".join(segment.text for segment in segments)

    result = _local_model.transcribe(str(audio_path), language="sv", initial_prompt=prompt)
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
    Läser valfritt ljudformat (via ffmpeg, som resten av appen) som
    16 kHz mono float32 - det format Pianissimo tränats på.

    ffmpeg gör om ljudet direkt till 16 kHz mono, så att bara det hamnar i
    minnet (ca 0,7 GB för 2 timmar) - inte hela klippet i full kvalitet först,
    som när pydub användes.

    Args:
        audio_path: Ljudfilen.

    Returns:
        En numpy-array med värden mellan -1 och 1, 16 000 värden per sekund.

    Raises:
        RuntimeError: Om ffmpeg inte kan läsa filen.
    """
    import subprocess

    import numpy as np
    from pydub import AudioSegment

    result = subprocess.run(
        [
            AudioSegment.converter, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", str(audio_path), "-vn", "-ac", "1", "-ar", str(_PIANISSIMO_SAMPLE_RATE),
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or "ffmpeg kunde inte läsa ljudet")
    return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0


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
