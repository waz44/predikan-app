"""
Tester för P2/P3-förbättringarna:
- Uppladdningsgräns (413) och att den halvskrivna filen städas bort
- /api/version returnerar appens version
- Setup-endpointsens localhost-skydd (_require_local_access)
- Setup-save skriver .env OCH slår igenom via config.reload() (e2e)
"""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import config
from modules import env_file
from routers import setup as setup_router


def _request_from(host: str | None) -> Request:
    """Bygger ett minimalt Starlette-Request med (eller utan) klient-host."""
    scope: dict = {"type": "http", "headers": []}
    if host is not None:
        scope["client"] = (host, 12345)
    return Request(scope)


def test_upload_rejects_too_large_file(client, tmp_env, monkeypatch):
    """
    En fil över uppladdningsgränsen avvisas med 413, och den halvt
    nedskrivna filen tas bort från uploads/.
    """
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 4)
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)  # bara för feltextens skull

    r = client.post("/api/upload", files={"file": ("sermon.mp3", b"123456789", "audio/mpeg")})
    assert r.status_code == 413
    # Den halvskrivna filen ska inte ligga kvar i uploads/.
    assert list(tmp_env["upload_dir"].iterdir()) == []


def test_version_endpoint(client):
    """
    /api/version svarar med appens version (från pyproject.toml).
    """
    r = client.get("/api/version")
    assert r.status_code == 200
    assert r.json()["version"] == config.VERSION
    assert config.VERSION  # inte tom


def test_setup_local_guard_blocks_remote(monkeypatch):
    """
    Inställningsguiden nekar anrop från en annan dator (403).
    """
    monkeypatch.setattr(config, "SETUP_ALLOW_REMOTE", False)
    with pytest.raises(HTTPException) as exc:
        setup_router._require_local_access(_request_from("10.1.2.3"))
    assert exc.value.status_code == 403


def test_setup_local_guard_allows_loopback(monkeypatch):
    """
    Anrop från samma dator (127.0.0.1) släpps igenom.
    """
    monkeypatch.setattr(config, "SETUP_ALLOW_REMOTE", False)
    # Ska inte kasta (returnerar None implicit).
    setup_router._require_local_access(_request_from("127.0.0.1"))


def test_setup_allow_remote_bypasses_guard(monkeypatch):
    """
    Med SETUP_ALLOW_REMOTE=true släpps även andra datorer igenom.
    """
    monkeypatch.setattr(config, "SETUP_ALLOW_REMOTE", True)
    setup_router._require_local_access(_request_from("203.0.113.9"))


def test_setup_save_writes_env_and_reloads(client, tmp_path, monkeypatch):
    """E2e: /api/setup/save skriver .env och config.reload() plockar upp värdet live."""
    env_path = tmp_path / ".env"
    env_path.write_text("SPREAKER_SHOW_ID=\n", encoding="utf-8")
    monkeypatch.setattr(env_file, "ENV_PATH", env_path)
    monkeypatch.setattr(env_file, "_EXAMPLE_PATH", tmp_path / ".env-example")
    monkeypatch.setattr(config, "_ENV_PATH", env_path)
    # config.reload() nedan gör load_dotenv(override=True), som skriver till
    # os.environ och config-globalen. Registrera dem hos monkeypatch så de
    # återställs efter testet och inte läcker till andra tester.
    monkeypatch.setenv("SPREAKER_SHOW_ID", "")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "")

    r = client.post("/api/setup/save", json={"values": {"SPREAKER_SHOW_ID": "998877"}})
    assert r.status_code == 200

    # Skrevs till .env ...
    assert "SPREAKER_SHOW_ID=998877" in env_path.read_text(encoding="utf-8")
    # ... och slog igenom live i config.
    assert config.SPREAKER_SHOW_ID == "998877"
