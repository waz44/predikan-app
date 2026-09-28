"""
Modul: audio_processor
Ansvarar för att trimma (klippa) och volymnormalisera ljudfiler, läsa deras
längd och räkna fram vågformen som visas i steg 2.

Allt görs med ffmpeg i ett flöde, utan att hela ljudet läses in i minnet.
Tidigare användes pydub, som håller hela filen okomprimerad i minnet (flera
kopior under klippningen) - en predikan på 2 timmar i wav (48 kHz, 24 bit,
ca 2 GB) behövde då ca 9 GB RAM och drygt 4 minuter bara för att klippas.
pydub används fortfarande för att hitta ffmpeg (AudioSegment.converter).
"""
# array: råa 16-bitars ljudvärden från ffmpeg, för vågformen.
import array

# re: läser toppnivån ur ffmpegs volumedetect-utskrift.
import re

# subprocess: kör ffmpeg.
import subprocess

# lru_cache: vågform och längd räknas bara fram en gång per fil (se _analyze).
from functools import lru_cache

# Path: sökvägar till in- och utfiler.
from pathlib import Path

# AudioSegment.converter: sökvägen till ffmpeg (hittas via PATH eller pydubs inställning).
from pydub import AudioSegment

# Samplingsfrekvens när vågformen och längden räknas fram. Låg, eftersom
# bara toppnivåerna behövs - men tillräcklig för en längd exakt på ms.
_ANALYSIS_SAMPLE_RATE = 4000
# Antal punkter per sekund i vågformen. 10 räcker gott för vågformens bredd
# och ger ca 72 000 värden (ca 400 kB JSON) för en predikan på 2 timmar.
PEAKS_PER_SECOND = 10
# Samma marginal under maxnivån som pydubs normalize() hade (0,1 dB).
_NORMALIZE_HEADROOM_DB = 0.1


def _run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    """
    Kör ffmpeg med de givna argumenten och kastar ett begripligt fel om det misslyckas.

    Args:
        args: Allt efter själva ffmpeg-kommandot.

    Returns:
        Resultatet, med ffmpegs utskrift (stderr) som text.

    Raises:
        RuntimeError: Om ffmpeg avslutas med fel (felet från ffmpeg ingår).
    """
    result = subprocess.run(
        [AudioSegment.converter, "-hide_banner", "-nostdin", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        # Sista raderna räcker - början av utskriften är bara filinformation.
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise RuntimeError(tail or f"ffmpeg avslutades med kod {result.returncode}")
    return result


def _peak(values: array.array) -> float:
    """Största utslaget (positivt eller negativt) som ett värde mellan 0 och 1."""
    return round(max(max(values), -min(values)) / 32768, 3)


@lru_cache(maxsize=16)
def _analyze_cached(path_str: str, mtime_ns: int, size: int) -> tuple[float, tuple[float, ...]]:
    """
    Avkodar filen EN gång (till mono med låg samplingsfrekvens, i ett flöde)
    och räknar fram både den exakta längden och vågformens toppnivåer.

    mtime_ns och size ingår bara för att en ändrad fil med samma namn inte
    ska ge ett gammalt resultat ur cachen.

    Returns:
        (längd i sekunder, toppnivåer mellan 0 och 1 - PEAKS_PER_SECOND per sekund)

    Raises:
        RuntimeError: Om ffmpeg inte kan läsa filen.
    """
    samples_per_peak = _ANALYSIS_SAMPLE_RATE // PEAKS_PER_SECOND
    # 16 bitar = 2 byte per värde.
    peak_bytes = samples_per_peak * 2
    proc = subprocess.Popen(
        [
            AudioSegment.converter, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", path_str, "-vn", "-ac", "1", "-ar", str(_ANALYSIS_SAMPLE_RATE),
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    peaks: list[float] = []
    total_samples = 0
    rest = b""
    # Läs ca 200 kB i taget och gör en toppnivå av varje 1/PEAKS_PER_SECOND sekund.
    while block := proc.stdout.read(peak_bytes * 256):
        data = rest + block
        usable = len(data) - len(data) % peak_bytes
        rest = data[usable:]
        values = array.array("h", data[:usable])
        total_samples += len(values)
        for i in range(0, len(values), samples_per_peak):
            peaks.append(_peak(values[i:i + samples_per_peak]))
    # Sista, ofullständiga tiondelen.
    rest = rest[: len(rest) - len(rest) % 2]
    if rest:
        values = array.array("h", rest)
        total_samples += len(values)
        peaks.append(_peak(values))
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    if proc.wait() != 0 or total_samples == 0:
        raise RuntimeError(stderr.strip() or "ffmpeg hittade inget ljud i filen")
    return total_samples / _ANALYSIS_SAMPLE_RATE, tuple(peaks)


def _analyze(path: Path) -> tuple[float, tuple[float, ...]]:
    """Längd och vågform för en fil (se _analyze_cached), cachat per fil och ändringstid."""
    stat = path.stat()
    return _analyze_cached(str(path), stat.st_mtime_ns, stat.st_size)


def get_waveform_peaks(path: Path) -> list[float]:
    """
    Vågformen för steg 2: toppnivån (0-1) för varje 1/PEAKS_PER_SECOND sekund.

    Räknas fram på servern så att webbläsaren slipper ladda ner och avkoda
    hela filen bara för att rita vågformen - det klarar den inte för en lång
    wav-fil (2 GB ljud blir flera GB i webbläsarens minne).

    Args:
        path: Ljudfilen, i valfritt format som ffmpeg kan läsa.

    Returns:
        Toppnivåerna i tidsordning.
    """
    return list(_analyze(path)[1])


def trim_and_normalize(
    input_path: Path,
    output_path: Path,
    start_seconds: float,
    end_seconds: float,
    export_format: str = "mp3",
) -> Path:
    """
    Klipper ljudfilen mellan start_seconds och end_seconds,
    normaliserar volymen och sparar resultatet.

    Görs i två omgångar med ffmpeg, båda i ett flöde utan att ljudet läses
    in i minnet: först mäts klippets starkaste topp (volumedetect), sedan
    höjs (eller sänks) hela klippet så att toppen hamnar strax under
    maxnivån - samma normalisering som pydubs normalize() gav tidigare.

    Args:
        input_path: Sökväg till originalfilen (alla format som ffmpeg klarar)
        output_path: Var den klippta filen ska sparas
        start_seconds: Startpunkt i sekunder
        end_seconds: Slutpunkt i sekunder (0 eller mindre = till slutet)
        export_format: "mp3" eller "wav"

    Returns:
        Sökvägen till den färdiga, klippta filen.

    Raises:
        ValueError: Om intervallet är tomt eller omvänt.
        RuntimeError: Om ffmpeg misslyckas.
    """
    duration_seconds = get_audio_duration_seconds(input_path)
    # Start får inte vara negativ.
    start = max(0.0, start_seconds)
    # Slut 0 (eller mindre) betyder "till slutet av filen", och slutet får
    # inte ligga efter filens slut.
    end = min(end_seconds if end_seconds > 0 else duration_seconds, duration_seconds)

    # Ett tomt eller omvänt intervall är ett fel i formuläret - säg det
    # tydligt i stället för att skapa en tom ljudfil. Jämförs på hela ms,
    # som när pydub användes.
    if int(start * 1000) >= int(end * 1000):
        raise ValueError(
            f"Ogiltigt intervall: start ({start_seconds}s) måste vara mindre "
            f"än slut ({end_seconds}s). Total längd: {duration_seconds:.1f}s"
        )

    # -ss före -i hoppar direkt till startpunkten (och avkodar sedan fram
    # till exakt rätt ställe), -t är klippets längd.
    clip_args = ["-ss", f"{start:.3f}", "-i", str(input_path), "-t", f"{end - start:.3f}", "-vn"]

    # Omgång 1: mät klippets starkaste topp i dB (0 dB = maxnivån).
    detect = _run_ffmpeg([*clip_args, "-af", "volumedetect", "-f", "null", "-"])
    match = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", detect.stderr)
    # Ett helt tyst klipp (-inf) lämnas som det är, precis som med pydubs normalize().
    gain_db = 0.0
    if match and match.group(1) != "-inf":
        gain_db = -float(match.group(1)) - _NORMALIZE_HEADROOM_DB

    # Skapa målmappen om den saknas (t.ex. processed/ vid första körningen).
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if export_format == "mp3":
        # 192 kbit/s ger bra kvalitet för tal med rimlig filstorlek
        # (ca 85 MB per timme) - Spreaker tar emot mp3 direkt.
        codec = ["-c:a", "libmp3lame", "-b:a", "192k"]
    else:
        # wav är okomprimerat - stora filer, men inga kvalitetsförluster.
        codec = ["-c:a", "pcm_s16le"]

    # Omgång 2: klipp, justera volymen och koda.
    try:
        _run_ffmpeg(["-y", *clip_args, "-af", f"volume={gain_db:.2f}dB", *codec, str(output_path)])
    except RuntimeError:
        # En halvskriven fil ska inte ligga kvar och förväxlas med ett resultat.
        output_path.unlink(missing_ok=True)
        raise

    return output_path


# --- Uppdelning i block för transkribering ---
# Transkriberingen görs block för block i stället för hela filen i ett
# stycke (se modules/transcription.py): varje block ryms med god marginal
# inom tjänsternas storleksgräns (25 MB hos Groq och OpenAI), ett fel eller
# en väntan på gratiskvoten gäller bara ett block, och framstegen kan visas
# som "block 3 av 12".
TRANSCRIPTION_BLOCK_SECONDS = 600
# Blocken delas helst i en paus i talet nära målet, så att inget ord klipps
# mitt itu: sökfönster före och efter målet (sekunder).
_CUT_SEARCH_BEFORE = 120
_CUT_SEARCH_AFTER = 60
# Ett sista block kortare än så här slås ihop med det föregående.
_MIN_LAST_BLOCK_SECONDS = 60
# Vad som räknas som en paus: under -35 dB i minst 0,4 sekunder.
_SILENCE_FILTER = "silencedetect=noise=-35dB:d=0.4"


def _detect_silences(path: Path) -> list[tuple[float, float]]:
    """
    Pauserna i en fil, som (start, slut) i sekunder, enligt ffmpegs silencedetect.

    Args:
        path: Ljudfilen.

    Returns:
        Pauserna i tidsordning.
    """
    result = _run_ffmpeg(["-i", str(path), "-vn", "-af", _SILENCE_FILTER, "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", result.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*(-?[\d.]+)", result.stderr)]
    return list(zip(starts, ends, strict=False))


def choose_cut_points(
    duration: float,
    silences: list[tuple[float, float]],
    block_seconds: float | None = None,
) -> list[float]:
    """
    Var ljudet ska delas: ungefär var block_seconds, helst mitt i en paus.

    För varje block letas den paus vars mitt ligger närmast målet inom
    sökfönstret. Finns ingen paus där delas det exakt vid målet.

    Args:
        duration: Filens längd i sekunder.
        silences: Pauserna, som (start, slut) i sekunder.
        block_seconds: Önskad blocklängd (standard TRANSCRIPTION_BLOCK_SECONDS).

    Returns:
        Delningspunkterna i sekunder (tom lista = ett enda block).
    """
    block_seconds = block_seconds or TRANSCRIPTION_BLOCK_SECONDS
    cuts: list[float] = []
    position = 0.0
    while duration - position > block_seconds + _MIN_LAST_BLOCK_SECONDS:
        target = position + block_seconds
        middles = [
            (start + end) / 2 for start, end in silences
            if target - _CUT_SEARCH_BEFORE <= (start + end) / 2 <= target + _CUT_SEARCH_AFTER
        ]
        cut = min(middles, key=lambda m: abs(m - target)) if middles else target
        cuts.append(round(cut, 3))
        position = cut
    return cuts


def split_for_transcription(path: Path, out_dir: Path, codec: str = "mp3") -> list[Path]:
    """
    Delar ljudet i block om ca TRANSCRIPTION_BLOCK_SECONDS, i pauser i talet.

    Blocken sparas i 16 kHz mono - det Whisper ändå arbetar i - så att de
    blir små: som mp3 (48 kbit/s) ca 3,6 MB per 10 minuter, vilket ryms
    gott inom tjänsternas gräns på 25 MB.

    Args:
        path: Ljudfilen (t.ex. den klippta predikan).
        out_dir: Mapp där blocken sparas (anroparen städar bort den).
        codec: "mp3" för tjänster på nätet, "wav" för lokal transkribering.

    Returns:
        Blockens sökvägar i tidsordning.

    Raises:
        RuntimeError: Om ffmpeg misslyckas.
    """
    duration = get_audio_duration_seconds(path)
    silences = _detect_silences(path) if duration > TRANSCRIPTION_BLOCK_SECONDS + _MIN_LAST_BLOCK_SECONDS else []
    bounds = [0.0, *choose_cut_points(duration, silences), duration]

    if codec == "mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", "48k"]
    else:
        codec_args = ["-c:a", "pcm_s16le"]

    out_dir.mkdir(parents=True, exist_ok=True)
    blocks = []
    for index, (start, end) in enumerate(zip(bounds, bounds[1:], strict=False), start=1):
        block = out_dir / f"block-{index:03d}.{codec}"
        _run_ffmpeg([
            "-y", "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{end - start:.3f}",
            "-vn", "-ac", "1", "-ar", "16000", *codec_args, str(block),
        ])
        blocks.append(block)
    return blocks


# Mål för normalize_loudness: -16 LUFS är gängse nivå för tal/poddar,
# -1.5 dBTP lämnar lite marginal så att mp3-kodningen inte klipper toppar.
LOUDNESS_TARGET_LUFS = -16
TRUE_PEAK_DB = -1.5


def normalize_loudness(input_path: Path, output_path: Path) -> Path:
    """
    Normaliserar hela filens upplevda ljudstyrka (EBU R128, ffmpegs loudnorm)
    och sparar resultatet som mp3.

    Används FÖRE klippningen (knappen "Normalisera ljud" i steg 2), så att
    en tyst inspelning blir lätt att lyssna på och se i vågformen när man
    letar klippunkter. Till skillnad från normaliseringen i trim_and_normalize
    (som bara lyfter den starkaste toppen) jämnar loudnorm ut nivån över tid,
    vilket gör större skillnad för tal med enstaka höga ljud (t.ex. musik).

    Args:
        input_path: Originalfilen, i valfritt format som ffmpeg kan läsa.
        output_path: Var den normaliserade mp3-filen ska sparas.

    Returns:
        Sökvägen till den normaliserade filen.

    Raises:
        RuntimeError: Om ffmpeg misslyckas (felet från ffmpeg ingår).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_ffmpeg([
            "-y", "-i", str(input_path),
            # Bara ljudet - omslagsbilder i mp3/m4a ska inte följa med.
            "-vn",
            "-af", f"loudnorm=I={LOUDNESS_TARGET_LUFS}:TP={TRUE_PEAK_DB}:LRA=11",
            # loudnorm räknar internt i 192 kHz - sätt tillbaka en normal samplingsfrekvens.
            "-ar", "44100",
            # Samma kvalitet som den klippta filen (se trim_and_normalize).
            "-c:a", "libmp3lame", "-b:a", "192k",
            str(output_path),
        ])
    except RuntimeError:
        # En halvskriven fil ska inte ligga kvar och förväxlas med ett resultat.
        output_path.unlink(missing_ok=True)
        raise
    return output_path


def get_audio_duration_seconds(path: Path) -> float:
    """
    Returnerar ljudfilens totala längd i sekunder (används av frontend).

    Används också för bulkimport (hela filen publiceras, så slutpunkten är
    filens längd) och för att uppskatta transkriberingstiden vid "Generera om".

    Args:
        path: Ljudfilen, i valfritt format som ffmpeg kan läsa.

    Returns:
        Längden i sekunder, med millisekunder som decimaler.

    Raises:
        RuntimeError: Om ffmpeg inte kan läsa filen.
    """
    # Hela filen avkodas för att få en exakt längd - metadata i filen (t.ex.
    # mp3-taggar) är ibland fel. Vågformen räknas fram samtidigt och cachas,
    # så att steg 2 får den direkt efter uppladdningen.
    return round(_analyze(path)[0], 3)
