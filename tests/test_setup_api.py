"""
Tester för routers/setup.py (inställningsguiden). Mockar bort .env-skrivning,
config.reload och externa Spreaker-/OpenAI-anrop, så testerna bara verifierar
routerns egen logik (maskering, nyckel-whitelist, hemlighets-skip, kod-extraktion).
"""
import pytest

import config
from routers import setup


@pytest.fixture
def no_side_effects(monkeypatch):
    """Fångar .env-skrivningar och stoppar config.reload från att röra riktig .env."""
    written = {}
    monkeypatch.setattr(setup.env_file, "set_values", lambda updates: written.update(updates))
    monkeypatch.setattr(setup.config, "reload", lambda: None)
    return written


def test_config_masks_secrets(client, monkeypatch):
    """
    Hemligheter skickas aldrig i klartext: bara om de är satta och en
    maskerad version med de sista tecknen.
    """
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "abcd1234secret")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    res = client.get("/api/setup/config")
    assert res.status_code == 200
    data = res.json()
    assert data["spreaker_api_token_set"] is True
    assert data["spreaker_api_token_masked"].endswith("cret")
    assert "secret" not in data["spreaker_api_token_masked"]
    assert data["openai_api_key_set"] is False


def test_save_rejects_unknown_key(client, no_side_effects):
    """
    Nycklar som inte är tillåtna avvisas, och ingenting skrivs till .env.
    """
    res = client.post("/api/setup/save", json={"values": {"EVIL_KEY": "x"}})
    assert res.status_code == 400
    assert not no_side_effects  # inget skrevs


def test_save_writes_whitelisted_values(client, no_side_effects):
    """
    Tillåtna nycklar sparas med sina värden.
    """
    res = client.post("/api/setup/save", json={"values": {"AI_PROVIDER": "ollama", "MAX_STORED_EPISODES": "5"}})
    assert res.status_code == 200
    assert no_side_effects["AI_PROVIDER"] == "ollama"
    assert no_side_effects["MAX_STORED_EPISODES"] == "5"


def test_archive_dir_can_be_saved_and_reloaded(client, no_side_effects, monkeypatch):
    """
    ARCHIVE_DIR kan sparas från inställningssidan.
    """
    res = client.post("/api/setup/save", json={"values": {"ARCHIVE_DIR": "D:/Podcastarkiv"}})
    assert res.status_code == 200
    assert no_side_effects["ARCHIVE_DIR"] == "D:/Podcastarkiv"


def test_reload_picks_up_archive_dir(tmp_path, monkeypatch):
    """ARCHIVE_DIR ingår i config.reload(), så en sparad ändring gäller utan omstart."""
    # reload() skriver om många modulvariabler - återställ dem efteråt så
    # övriga tester inte påverkas, och läs inte in den riktiga .env-filen.
    snapshot = {k: v for k, v in vars(config).items() if k.isupper()}
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **kw: None)
    try:
        monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "annan_disk"))
        config.reload()
        assert config.ARCHIVE_DIR == tmp_path / "annan_disk"

        monkeypatch.setenv("ARCHIVE_DIR", "")
        config.reload()
        assert config.ARCHIVE_DIR == config.BASE_DIR / "podcast_arkiv"
    finally:
        vars(config).update(snapshot)


def test_save_skips_empty_secret(client, no_side_effects):
    """Ett tomt hemligt fält ska INTE nollställa en redan sparad hemlighet."""
    res = client.post(
        "/api/setup/save",
        json={"values": {"SPREAKER_API_TOKEN": "", "SPREAKER_SHOW_ID": "77"}},
    )
    assert res.status_code == 200
    assert "SPREAKER_API_TOKEN" not in no_side_effects
    assert no_side_effects["SPREAKER_SHOW_ID"] == "77"


def test_authorize_url(client):
    """
    Länken till Spreakers godkännandesida innehåller appens Client ID.
    """
    res = client.post(
        "/api/setup/spreaker/authorize-url",
        json={"client_id": "myid", "redirect_uri": "http://localhost"},
    )
    assert res.status_code == 200
    url = res.json()["url"]
    assert "client_id=myid" in url
    assert "oauth2/authorize" in url


def test_exchange_extracts_code_from_url(client, monkeypatch):
    """
    Klistras hela adressen från adressfältet in plockas koden ut ur den,
    och svaret innehåller token och kontots shows.
    """
    captured = {}

    def fake_exchange(client_id, client_secret, redirect_uri, code):
        captured["code"] = code
        return "tok-123"

    monkeypatch.setattr(setup.spreaker_client, "exchange_oauth_code", fake_exchange)
    monkeypatch.setattr(setup.spreaker_client, "get_me", lambda t: {"fullname": "Test User", "user_id": 1})
    monkeypatch.setattr(setup.spreaker_client, "list_my_shows", lambda t: [{"show_id": 42, "title": "Min podd"}])

    res = client.post(
        "/api/setup/spreaker/exchange",
        json={
            "client_id": "id",
            "client_secret": "secret",
            "redirect_uri": "http://localhost",
            "code": "http://localhost/?state=xyz&code=THE_CODE",
        },
    )
    assert res.status_code == 200
    assert captured["code"] == "THE_CODE"  # koden plockades ur URL:en
    data = res.json()
    assert data["token"] == "tok-123"
    assert data["shows"][0]["show_id"] == 42


def test_verify_token(client, monkeypatch):
    """
    En befintlig token kontrolleras och kontots användare returneras.
    """
    monkeypatch.setattr(setup.spreaker_client, "get_me", lambda t: {"fullname": "Anna", "user_id": 2})
    monkeypatch.setattr(setup.spreaker_client, "list_my_shows", lambda t: [])
    res = client.post("/api/setup/spreaker/verify", json={"token": "sometoken"})
    assert res.status_code == 200
    assert res.json()["user"]["fullname"] == "Anna"
