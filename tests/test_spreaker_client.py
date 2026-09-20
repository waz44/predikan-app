"""
Tester för modules/spreaker_client.py:publish_episode - specifikt den
RIKTIGA (icke-simulerade) grenen, som ingen tidigare test täckte (alla
andra tester kör med SPREAKER_SIMULATE=true, se tests/conftest.py, vilket
alltid tar simulerings-grenen istället).
"""
import pytest
import requests

import config
from modules import spreaker_client


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeEncoder:
    """Ersätter MultipartEncoder - fångar bara vilka fields den byggdes med, bygger ingen riktig multipart-body."""
    def __init__(self, fields):
        self.fields = fields
        self.len = 10
        self.content_type = "multipart/form-data; boundary=x"


class _FakeMonitor:
    def __init__(self, encoder, callback):
        self.encoder = encoder
        self.callback = callback
        self.len = encoder.len
        self.content_type = encoder.content_type
        self.bytes_read = encoder.len


def test_publish_episode_sends_description_as_plain_text(tmp_env, monkeypatch, tmp_path):
    """
    Description ska skickas OFÖRÄNDRAD till Spreaker - inte HTML-formaterad.
    Verifierat mot ett riktigt konto: Spreakers "description"-fält är rent
    text, och HTML-taggar som skickas dit stryks tyst bort av Spreaker
    (som redan själva genererar ett separat "description_html"-fält
    utifrån texten) - se modules/text_formatting.py för bakgrunden.
    """
    monkeypatch.setattr(config, "SPREAKER_SIMULATE", False)
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "tok")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "123")

    captured = {}

    def _make_fake_encoder(fields):
        captured["fields"] = fields
        return _FakeEncoder(fields)

    monkeypatch.setattr(spreaker_client, "MultipartEncoder", _make_fake_encoder)
    monkeypatch.setattr(spreaker_client, "MultipartEncoderMonitor", _FakeMonitor)
    monkeypatch.setattr(
        requests, "post",
        lambda url, headers=None, data=None, timeout=None: _FakeResponse(
            200, {"response": {"episode": {"episode_id": 1, "site_url": "https://x/1"}}}
        ),
    )

    audio_path = tmp_path / "clip.mp3"
    audio_path.write_bytes(b"fake-audio")

    result = spreaker_client.publish_episode(
        audio_path=audio_path, title="Titel", description="Rad 1\n\nRad 2", tags=[],
    )

    assert captured["fields"]["description"] == "Rad 1\n\nRad 2"
    assert result["episode_id"] == 1
    assert result["simulated"] is False


class _FakeStreamResponse:
    """Ersätter en requests-respons med .iter_content() (streamat nedladdningssvar)."""
    def __init__(self, status_code, chunks):
        self.status_code = status_code
        self._chunks = chunks
        self.text = ""

    def iter_content(self, chunk_size=None):
        yield from self._chunks


def test_download_episode_audio_streams_to_file(tmp_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "tok")

    def fake_get(url, headers=None, timeout=None, stream=None):
        assert "75191712" in url
        assert headers["Authorization"] == "Bearer tok"
        return _FakeStreamResponse(200, [b"abc", b"def"])

    monkeypatch.setattr(requests, "get", fake_get)

    dest = tmp_path / "sub" / "episode.mp3"
    spreaker_client.download_episode_audio(75191712, dest)

    assert dest.read_bytes() == b"abcdef"


def test_download_episode_audio_failure_raises(tmp_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "tok")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeStreamResponse(404, []))

    with pytest.raises(spreaker_client.SpreakerUploadError):
        spreaker_client.download_episode_audio(1, tmp_path / "x.mp3")
