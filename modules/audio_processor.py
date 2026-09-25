"""
Modul: audio_processor
Ansvarar för att trimma (klippa) och volymnormalisera ljudfiler med pydub
(som i sin tur kräver att ffmpeg finns installerat på systemet).
"""
# Path: sökvägar till in- och utfiler.
from pathlib import Path

# AudioSegment: pydubs representation av ett helt ljudklipp i minnet.
# Det kan klippas som en lista: audio[start_ms:slut_ms].
from pydub import AudioSegment

# normalize: höjer (eller sänker) hela klippet så att den starkaste
# punkten hamnar strax under maxnivån - utan att förvränga ljudet.
from pydub.effects import normalize


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

    Args:
        input_path: Sökväg till originalfilen (.mp3 eller .wav)
        output_path: Var den klippta filen ska sparas
        start_seconds: Startpunkt i sekunder
        end_seconds: Slutpunkt i sekunder
        export_format: "mp3" eller "wav"

    Returns:
        Sökvägen till den färdiga, klippta filen.
    """
    # Läser in hela filen via ffmpeg - alla format som ffmpeg klarar fungerar
    # (mp3, wav, m4a, flac, ogg, wma ...).
    audio = AudioSegment.from_file(input_path)

    # pydub räknar i millisekunder: len(audio) är längden i ms.
    duration_seconds = len(audio) / 1000.0
    # Start får inte vara negativ.
    start_ms = max(0, int(start_seconds * 1000))
    # Slut 0 (eller mindre) betyder "till slutet av filen".
    end_ms = int(end_seconds * 1000) if end_seconds > 0 else len(audio)
    # Och slutet får inte ligga efter filens slut.
    end_ms = min(end_ms, len(audio))

    # Ett tomt eller omvänt intervall är ett fel i formuläret - säg det
    # tydligt i stället för att skapa en tom ljudfil.
    if start_ms >= end_ms:
        raise ValueError(
            f"Ogiltigt intervall: start ({start_seconds}s) måste vara mindre "
            f"än slut ({end_seconds}s). Total längd: {duration_seconds:.1f}s"
        )

    # Klipp ut den valda delen.
    clipped = audio[start_ms:end_ms]

    # Normalisera volymen så att predikan har jämn, konsekvent ljudnivå
    normalized = normalize(clipped)

    # Skapa målmappen om den saknas (t.ex. processed/ vid första körningen).
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if export_format == "mp3":
        # 192 kbit/s ger bra kvalitet för tal med rimlig filstorlek
        # (ca 85 MB per timme) - Spreaker tar emot mp3 direkt.
        normalized.export(output_path, format="mp3", bitrate="192k")
    else:
        # wav är okomprimerat - stora filer, men inga kvalitetsförluster.
        normalized.export(output_path, format="wav")

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
    """
    # Hela filen läses in för att få en exakt längd. Det tar någon sekund för
    # en lång predikan, men metadata i filen (t.ex. mp3-taggar) är ibland fel.
    audio = AudioSegment.from_file(path)
    # Millisekunder -> sekunder.
    return len(audio) / 1000.0
