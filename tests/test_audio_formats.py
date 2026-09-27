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


def test_normalize_replaces_upload_directly(client, tmp_path):
    """
    "Normalisera ljud" körs direkt (inte via kön): filen bakom file_id byts
    mot en normaliserad mp3 och originalet tas bort.
    """
    from services import state

    wav_path = tmp_path / "quiet.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-af", "volume=0.05", "-ar", "8000", "-ac", "1", str(wav_path),
            "-loglevel", "error",
        ],
        check=True,
    )
    with open(wav_path, "rb") as f:
        file_id = client.post("/api/upload", files={"file": ("quiet.wav", f, "audio/wav")}).json()["file_id"]
    original = state.UPLOADED_FILES[file_id]

    r = client.post(f"/api/audio/{file_id}/normalize")

    assert r.status_code == 200
    assert r.json()["duration_seconds"] == pytest.approx(2.0, abs=0.2)
    normalized = state.UPLOADED_FILES[file_id]
    assert normalized.suffix == ".mp3" and normalized.exists()
    assert not original.exists()
    # Inget hamnade i bearbetningskön.
    assert client.get("/api/queue").json()["items"] == []


def test_normalize_unknown_file_is_404(client):
    r = client.post("/api/audio/finns-inte/normalize")
    assert r.status_code == 404
