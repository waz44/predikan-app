"""
Tester för "Hantera Spreaker"-fliken (routers/spreaker_episodes.py +
modules/spreaker_episode_store.py). Till skillnad från publiceringsflödet
finns ingen SPREAKER_SIMULATE-väg här - requests.get/requests.post
monkeypatchas därför direkt, med en enkel fejk-respons.
"""
import requests

import config
from modules.spreaker_episode_store import _extract_speaker


class _FakeResponse:
    """
    Låtsassvar från requests: statuskod och ett JSON-innehåll.
    """
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _configure_real_spreaker(monkeypatch):
    """
    Ställer in token, show-id och simulering av, så att avsnittshanteringen är påslagen.
    """
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "tok")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "123")
    monkeypatch.setattr(config, "SPREAKER_SIMULATE", False)


def test_extract_speaker_various_cases():
    """
    Talaren läses ur raden "Talare: X" oavsett versaler; saknas raden blir
    det None, och finns flera rader gäller den sista.
    """
    assert _extract_speaker(None) is None
    assert _extract_speaker("") is None
    assert _extract_speaker("Bara text, ingen talarrad.") is None
    assert _extract_speaker("Text\nTalare: Anna Andersson") == "Anna Andersson"
    assert _extract_speaker("Text\ntalare: gemener funkar också") == "gemener funkar också"
    # Flera "Talare:"-rader (t.ex. efter manuell redigering) - SISTA raden vinner.
    assert _extract_speaker("Text\nTalare: Först\nTalare: Sist") == "Sist"


def test_status_reports_configured_state(client, tmp_env, monkeypatch):
    """
    Status: avsnittslistan kräver token, show-id och simulering av, medan
    arkivet bara kräver show-id.
    """
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "")
    assert client.get("/api/spreaker/status").json() == {"configured": False, "archive_available": False}

    _configure_real_spreaker(monkeypatch)
    assert client.get("/api/spreaker/status").json() == {"configured": True, "archive_available": True}

    # SIMULATE=true ska dölja/stänga av hanteringen även om token/show-id finns.
    # Arkivet läser bara det publika RSS-flödet och kräver bara show-id.
    monkeypatch.setattr(config, "SPREAKER_SIMULATE", True)
    assert client.get("/api/spreaker/status").json() == {"configured": False, "archive_available": True}


def test_episode_endpoints_return_403_when_not_configured(client, tmp_env, monkeypatch):
    """
    Utan inställningar svarar alla avsnittsanrop 403 - även om någon anropar
    dem direkt, förbi den dolda fliken.
    """
    monkeypatch.setattr(config, "SPREAKER_API_TOKEN", "")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "")

    assert client.get("/api/spreaker/episodes").status_code == 403
    assert client.post("/api/spreaker/episodes/fetch").status_code == 403
    assert client.put("/api/spreaker/episodes/1", json={"title": "x"}).status_code == 403


def test_fetch_episodes_paginates_and_parses_speaker(client, tmp_env, monkeypatch):
    """
    Listnings-svaret (GET .../episodes) saknar description/plays_count i
    praktiken - bara episode_id/title/duration/published_at/site_url finns
    där. list_episodes() måste därför göra ETT extra anrop per avsnitt mot
    GET /v2/episodes/{id} (som DÄREMOT har allt) för att Talare och
    beskrivning ska gå att visa - detta test speglar precis det verkliga
    svarsformatet för båda endpointerna, inte en förenklad variant.
    """
    _configure_real_spreaker(monkeypatch)

    list_page1 = {
        "response": {
            "items": [
                {"episode_id": 1, "title": "Ep1", "duration": 60000, "published_at": "2026-01-01 10:00:00", "site_url": "https://x/1"},
            ],
            "next_url": "https://api.spreaker.com/v2/shows/123/episodes?page=2",
        }
    }
    list_page2 = {
        "response": {
            "items": [
                {"episode_id": 2, "title": "Ep2", "duration": 30000, "published_at": "2026-01-02 10:00:00", "site_url": "https://x/2"},
            ],
            "next_url": None,
        }
    }
    detail_1 = {
        "response": {"episode": {
            "episode_id": 1, "title": "Ep1", "description": "Text\nTalare: Anna",
            "duration": 60000, "published_at": "2026-01-01 10:00:00",
            "site_url": "https://x/1", "plays_count": 5,
        }}
    }
    detail_2 = {
        "response": {"episode": {
            "episode_id": 2, "title": "Ep2", "description": "Ingen talarrad här",
            "duration": 30000, "published_at": "2026-01-02 10:00:00",
            "site_url": "https://x/2", "plays_count": None,
        }}
    }

    calls = {"list": 0, "detail": 0}

    def fake_get(url, headers=None, timeout=None):
        if "/shows/" in url:
            calls["list"] += 1
            return _FakeResponse(200, list_page1 if calls["list"] == 1 else list_page2)
        calls["detail"] += 1
        episode_id = int(url.rstrip("/").rsplit("/", 1)[-1])
        return _FakeResponse(200, detail_1 if episode_id == 1 else detail_2)

    monkeypatch.setattr(requests, "get", fake_get)

    res = client.post("/api/spreaker/episodes/fetch")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 2
    assert calls["list"] == 2, "ska följa next_url tills den är null"
    assert calls["detail"] == 2, "ska hämta fullständiga fält per avsnitt (listan saknar description/plays_count)"

    by_id = {it["episode_id"]: it for it in items}
    assert by_id[1]["title"] == "Ep1"
    assert by_id[1]["description"] == "Text\nTalare: Anna"
    assert by_id[1]["speaker"] == "Anna"
    assert by_id[1]["duration_seconds"] == 60.0
    assert by_id[1]["plays_count"] == 5
    assert by_id[2]["description"] == "Ingen talarrad här"
    assert by_id[2]["speaker"] is None

    # Cachad vy (GET, inget nytt anrop) ska innehålla samma data.
    cached = client.get("/api/spreaker/episodes").json()["items"]
    cached_by_id = {it["episode_id"]: it for it in cached}
    assert cached_by_id[1]["description"] == "Text\nTalare: Anna"
    assert cached_by_id[1]["speaker"] == "Anna"


def test_fetch_failure_returns_502(client, tmp_env, monkeypatch):
    """
    Ett fel från Spreaker (här ogiltig token) blir 502 - felet kom utifrån.
    """
    _configure_real_spreaker(monkeypatch)
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse(401, {"error": "bad token"}))

    res = client.post("/api/spreaker/episodes/fetch")
    assert res.status_code == 502


def test_update_episode_saves_to_spreaker_and_local_cache(client, tmp_env, monkeypatch):
    """
    En ändring skickas till Spreaker (beskrivningen oförändrad, som ren
    text) och den lokala listan uppdateras - inklusive talaren, som läses
    ur den nya beskrivningen.
    """
    _configure_real_spreaker(monkeypatch)

    from modules import spreaker_episode_store
    spreaker_episode_store.replace_all([
        {
            "episode_id": 42, "title": "Gammal titel", "description": "Gammal\nTalare: Bertil",
            "duration": 10000, "published_at": "2026-01-01 00:00:00",
            "site_url": "https://x/42", "plays_count": 1,
        },
    ])

    sent = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        sent["url"] = url
        sent["data"] = data
        return _FakeResponse(200, {"response": {}})

    monkeypatch.setattr(requests, "post", fake_post)

    res = client.put("/api/spreaker/episodes/42", json={"title": "Ny titel", "description": "Ny text\nTalare: Cecilia"})
    assert res.status_code == 200
    assert sent["data"]["title"] == "Ny titel"
    assert "42" in sent["url"]
    # Description skickas OFÖRÄNDRAD - Spreakers "description"-fält är
    # rent text (verifierat mot ett riktigt konto: HTML-taggar stryks
    # tyst bort där), se modules/text_formatting.py för bakgrunden.
    assert sent["data"]["description"] == "Ny text\nTalare: Cecilia"

    cached = client.get("/api/spreaker/episodes").json()["items"]
    assert cached[0]["title"] == "Ny titel"
    assert cached[0]["description"] == "Ny text\nTalare: Cecilia"
    assert cached[0]["speaker"] == "Cecilia"


def test_update_episode_failure_returns_502(client, tmp_env, monkeypatch):
    """
    Avvisar Spreaker ändringen blir svaret 502.
    """
    _configure_real_spreaker(monkeypatch)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeResponse(500, {"error": "server error"}))

    res = client.put("/api/spreaker/episodes/1", json={"title": "x", "description": "y"})
    assert res.status_code == 502
