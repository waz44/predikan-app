"""
Modul: audio_processor
Ansvarar för att trimma (klippa) och volymnormalisera ljudfiler med pydub
(som i sin tur kräver att ffmpeg finns installerat på systemet).
"""
from pathlib import Path

from pydub import AudioSegment
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
    audio = AudioSegment.from_file(input_path)

    duration_seconds = len(audio) / 1000.0
    start_ms = max(0, int(start_seconds * 1000))
    end_ms = int(end_seconds * 1000) if end_seconds > 0 else len(audio)
    end_ms = min(end_ms, len(audio))

    if start_ms >= end_ms:
        raise ValueError(
            f"Ogiltigt intervall: start ({start_seconds}s) måste vara mindre "
            f"än slut ({end_seconds}s). Total längd: {duration_seconds:.1f}s"
        )

    clipped = audio[start_ms:end_ms]

    # Normalisera volymen så att predikan har jämn, konsekvent ljudnivå
    normalized = normalize(clipped)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if export_format == "mp3":
        normalized.export(output_path, format="mp3", bitrate="192k")
    else:
        normalized.export(output_path, format="wav")

    return output_path


def get_audio_duration_seconds(path: Path) -> float:
    """Returnerar ljudfilens totala längd i sekunder (används av frontend)."""
    audio = AudioSegment.from_file(path)
    return len(audio) / 1000.0
