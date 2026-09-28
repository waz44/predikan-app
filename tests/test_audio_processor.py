"""
Tester för modules/audio_processor.py (klippning, normalisering, längd och
vågform med ffmpeg i ett flöde) och för uppspelningen av långa filer i
routers/upload.py (Range-förfrågningar och vågformen från servern).
Kräver ffmpeg, som resten av appen (se README avsnitt 1).
"""
import subprocess

import pytest

from modules import audio_processor


def _make_tone(path, seconds, volume=0.1, rate=48000, codec="pcm_s24le"):
    """En ton i wav (stereo, 24 bit som standard - som en inspelning från ett mixerbord)."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
            "-af", f"volume={volume}", "-ac", "2", "-c:a", codec, str(path),
        ],
        check=True,
    )
    return path


def _max_volume_db(path):
    """Filens starkaste topp i dB enligt ffmpeg."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    return float(out.split("max_volume:")[1].split("dB")[0])


def test_duration_and_peaks(tmp_path):
    wav = _make_tone(tmp_path / "a.wav", 3)

    assert audio_processor.get_audio_duration_seconds(wav) == pytest.approx(3.0, abs=0.01)
    peaks = audio_processor.get_waveform_peaks(wav)
    assert len(peaks) == 3 * audio_processor.PEAKS_PER_SECOND
    # Vågformens högsta topp motsvarar filens starkaste topp enligt ffmpeg.
    assert max(peaks) == pytest.approx(10 ** (_max_volume_db(wav) / 20), rel=0.1)


def test_trim_cuts_and_normalizes(tmp_path):
    wav = _make_tone(tmp_path / "a.wav", 5)
    out = tmp_path / "out.mp3"

    audio_processor.trim_and_normalize(wav, out, 1.0, 3.5)

    assert audio_processor.get_audio_duration_seconds(out) == pytest.approx(2.5, abs=0.06)
    # Från ca -20 dB upp till strax under maxnivån.
    assert _max_volume_db(out) > -1.0


def test_trim_end_zero_means_whole_file_and_wav_export(tmp_path):
    wav = _make_tone(tmp_path / "a.wav", 2)
    out = tmp_path / "out.wav"

    audio_processor.trim_and_normalize(wav, out, 0, 0, export_format="wav")

    assert audio_processor.get_audio_duration_seconds(out) == pytest.approx(2.0, abs=0.01)


def test_trim_rejects_empty_interval(tmp_path):
    wav = _make_tone(tmp_path / "a.wav", 2)

    with pytest.raises(ValueError, match="Ogiltigt intervall"):
        audio_processor.trim_and_normalize(wav, tmp_path / "out.mp3", 5, 10)
    assert not (tmp_path / "out.mp3").exists()


def test_peaks_endpoint_and_range_playback(client, tmp_path):
    wav = _make_tone(tmp_path / "a.wav", 2, codec="pcm_s16le")
    size = wav.stat().st_size
    with open(wav, "rb") as f:
        file_id = client.post("/api/upload", files={"file": ("a.wav", f, "audio/wav")}).json()["file_id"]

    r = client.get(f"/api/audio/{file_id}/peaks")
    assert r.status_code == 200
    assert r.json()["duration_seconds"] == pytest.approx(2.0, abs=0.01)
    assert len(r.json()["peaks"]) == 2 * audio_processor.PEAKS_PER_SECOND

    # Utan Range: hela filen, och webbläsaren får veta att Range stöds.
    r = client.get(f"/api/audio/{file_id}")
    assert r.status_code == 200 and len(r.content) == size
    assert r.headers["accept-ranges"] == "bytes"

    # Med Range: bara den begärda delen.
    r = client.get(f"/api/audio/{file_id}", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{size}"
    assert r.content == wav.read_bytes()[100:200]

    # Öppet slut och "sista N byte".
    assert len(client.get(f"/api/audio/{file_id}", headers={"Range": f"bytes={size - 10}-"}).content) == 10
    assert client.get(f"/api/audio/{file_id}", headers={"Range": "bytes=-5"}).content == wav.read_bytes()[-5:]

    # Utanför filen.
    assert client.get(f"/api/audio/{file_id}", headers={"Range": f"bytes={size}-"}).status_code == 416
