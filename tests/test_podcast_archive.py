"""
Tester för podd-arkivet (modules/podcast_archive.py + /api/spreaker/archive/...).
requests.get monkeypatchas med ett fejkat RSS-flöde och fejkade mp3-svar,
så inga riktiga nätverksanrop görs.
"""
import time

import requests

import config
from modules import podcast_archive

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>Testpodden</title>
  <link>https://example.com</link>
  <item>
    <title>Anna Andersson: Nåd &amp; frid</title>
    <link>https://example.com/1</link>
    <guid>https://api.spreaker.com/episode/1</guid>
    <pubDate>Sun, 07 Sep 2025 09:30:00 +0000</pubDate>
    <description><![CDATA[<p>Om nåd.</p><p>Talare: Anna Andersson</p>]]></description>
    <enclosure url="https://example.com/1.mp3" length="6" type="audio/mpeg"/>
    <itunes:duration>3725</itunes:duration>
    <itunes:keywords>nåd,frid</itunes:keywords>
  </item>
  <item>
    <title>Utan ljud</title>
  </item>
</channel>
</rss>""".encode()


class _FakeResponse:
    """
    Låtsassvar från requests: statuskod, innehåll och Content-Length.

    Fungerar både som vanligt svar och med "with" (som _download använder).
    """
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content
        self.headers = {"Content-Length": str(len(content))}

    def iter_content(self, chunk_size):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_get(calls):
    """
    Ersätter requests.get: flödesadressen ger FEED, allt annat en liten mp3.

    Args:
        calls: Lista som fylls med de adresser som anropas.

    Returns:
        Funktionen som ersätter requests.get.
    """
    def fake(url, **kwargs):
        calls.append(url)
        if url.endswith("/episodes/feed"):
            return _FakeResponse(200, FEED)
        return _FakeResponse(200, b"ID3abc")
    return fake


def test_helpers_match_powershell_script():
    """
    Filnamn, talare och beskrivning blir exakt som i PowerShell-skriptet -
    annars skulle ett befintligt arkiv inte kännas igen.
    """
    assert podcast_archive.safe_filename('a:b/c?  [x]. ') == "a_b_c_ (x)"
    assert podcast_archive.split_title("Anna: Titel", "") == ("Anna", "Titel")
    assert podcast_archive.split_title("Titel - Anna", "x\nTalare: Anna") == ("Anna", "Titel")
    assert podcast_archive.clean_description("<p>A &amp;quot;b&amp;quot;</p><p>C</p>") == 'A "b"\r\n\r\nC'


def test_run_saves_mp3_xml_txt_and_skips_existing(tmp_env, tmp_path, monkeypatch):
    """
    En körning sparar mp3, xml och txt med rätt innehåll och loggar avsnitt
    utan ljudfil. En andra körning hämtar bara flödet - inget laddas ner igen.
    """
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "arkiv")
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "123")
    calls: list[str] = []
    monkeypatch.setattr(requests, "get", _fake_get(calls))

    podcast_archive.run()

    archive = tmp_path / "arkiv"
    mp3s = list(archive.glob("*.mp3"))
    assert len(mp3s) == 1
    name = mp3s[0].stem
    assert name.endswith("_Anna Andersson_Nåd & frid")
    assert mp3s[0].read_bytes() == b"ID3abc"
    txt = (archive / f"{name}.txt").read_text(encoding="utf-8-sig")
    assert "Talare:      Anna Andersson" in txt
    assert "Längd:       1:02:05" in txt
    assert "Nyckelord:   nåd, frid" in txt
    xml = (archive / f"{name}.xml").read_text(encoding="utf-8-sig")
    assert "<itunes:duration>3725</itunes:duration>" in xml
    assert "Testpodden" in xml
    assert "Ingen ljudfil: Utan ljud" in (archive / "logg.txt").read_text(encoding="utf-8-sig")
    assert calls[0] == "https://www.spreaker.com/show/123/episodes/feed"

    # Andra körningen: mp3:an finns redan -> bara flödet hämtas igen.
    calls.clear()
    podcast_archive.run()
    assert calls == ["https://www.spreaker.com/show/123/episodes/feed"]


def test_archive_endpoints(client, tmp_path, monkeypatch):
    """
    Arkivets API: 403 utan show-id, och med show-id går en körning igenom
    hela vägen till "klar" med ett nedladdat avsnitt och inga fel.
    """
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "")
    assert client.post("/api/spreaker/archive/run").status_code == 403

    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "123")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "arkiv")
    monkeypatch.setattr(requests, "get", _fake_get([]))

    assert client.post("/api/spreaker/archive/run").status_code == 200
    for _ in range(100):
        status = client.get("/api/spreaker/archive/status").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["running"] is False
    assert status["downloaded"] == 1
    assert status["failures"] == []
    assert status["error"] is None


# ---------------------------------------------------------------- koppling till Hantera Spreaker

def _archive_one_episode(tmp_env, monkeypatch):
    """Kör en arkivering av FEED (avsnitt 1) mot tmp_env:s arkivmapp."""
    monkeypatch.setattr(config, "SPREAKER_SHOW_ID", "123")
    monkeypatch.setattr(requests, "get", _fake_get([]))
    podcast_archive.run()
    return config.ARCHIVE_DIR


def test_index_links_archive_to_episode_id(tmp_env, monkeypatch):
    """
    Arkivets filer kopplas till Spreakers episode_id via <guid>. Transkript
    kan sparas och läsas för arkiverade avsnitt, men skapas aldrig för
    avsnitt som inte finns i arkivet.
    """
    archive = _archive_one_episode(tmp_env, monkeypatch)
    idx = podcast_archive.index()
    assert list(idx) == [1]
    assert podcast_archive.audio_path(1) == archive / (idx[1].name + ".mp3")
    assert podcast_archive.local_info(1) == {"archived": True, "has_transcript": False}
    assert podcast_archive.local_info(2) == {"archived": False, "has_transcript": False}

    assert podcast_archive.save_transcript(1, "Hela transkriptet.") is True
    assert podcast_archive.read_transcript(1) == "Hela transkriptet."
    assert podcast_archive.local_info(1)["has_transcript"] is True
    # Avsnitt som inte finns i arkivet får inga lösa transkriptfiler.
    assert podcast_archive.save_transcript(2, "x") is False


def test_run_exports_cached_transcript_to_archive(tmp_env, monkeypatch):
    """
    Ett transkript som redan finns i databasen följer med till arkivet vid
    nästa arkivering.
    """
    from modules import spreaker_episode_store

    spreaker_episode_store.save_transcript(1, "Transkript från databasen.")
    _archive_one_episode(tmp_env, monkeypatch)
    assert podcast_archive.read_transcript(1) == "Transkript från databasen."


def test_missing_archive_dir_gives_empty_index(tmp_env, monkeypatch):
    """
    En arkivmapp som saknas (t.ex. urkopplad disk) ger ett tomt index, inget fel.
    """
    monkeypatch.setattr(config, "ARCHIVE_DIR", config.ARCHIVE_DIR / "finns-inte")
    assert podcast_archive.index() == {}
    assert podcast_archive.audio_path(1) is None
