"""
Tester för stödet för fler ljudformat än mp3/wav vid uppladdning. Formaten
avkodas av ffmpeg (redan ett hårt krav för att appen ska fungera alls, se
README avsnitt 1) och normaliseras till mp3 i klippningssteget, så
originalformatet spelar ingen roll för resten av pipelinen.
"""
import subprocess

import pytest

import config


def test_common_formats_are_allowed():
    """
    De vanliga ljudformaten finns i listan över tillåtna filtyper.
    """
    for ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma"):
        assert ext in config.ALLOWED_EXTENSIONS


def test_uncommon_extension_is_rejected(client):
    """
    En fil som inte är ljud avvisas vid uppladdning med ett begripligt fel.
    """
    r = client.post("/api/upload", files={"file": ("sermon.txt", b"not audio", "text/plain")})
    assert r.status_code == 400
    assert "stöds ej" in r.json()["detail"]


def test_ogg_upload_is_decoded_correctly(client, tmp_path):
    """Verifierar att ett icke-mp3/wav-format faktiskt går att ladda upp och läsas (kräver ffmpeg, se README avsnitt 1)."""
    ogg_path = tmp_path / "sermon.ogg"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-ar", "8000", "-ac", "1", "-c:a", "libvorbis", str(ogg_path),
            "-loglevel", "error",
        ],
        check=True,
    )

    with open(ogg_path, "rb") as f:
        r = client.post("/api/upload", files={"file": ("sermon.ogg", f, "audio/ogg")})

    assert r.status_code == 200
    data = r.json()
    assert data["duration_seconds"] == pytest.approx(1.0, abs=0.2)
