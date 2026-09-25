"""
Modul: podcast_archive
Sparar ett lokalt arkiv av hela podden (motsvarar det fristående skriptet
ladda_podd.ps1). Läser showens PUBLIKA RSS-flöde hos Spreaker - ingen token
behövs, bara SPREAKER_SHOW_ID - och sparar per avsnitt i config.ARCHIVE_DIR:

    .mp3  - ljudet (laddas bara ner om det inte redan finns)
    .xml  - avsnittets originaldata ur flödet (skrivs om varje körning så
            ändringar i flödet följer med)
    .txt  - läsbar sammanställning: titel, talare, datum, längd, nyckelord,
            länkar och beskrivning

    .transkript.txt - hela transkriberingen, när avsnittet transkriberats
            (via "Generera om" i Hantera Spreaker, se services/pipeline.py)

En logg över varje körning sparas i <ARCHIVE_DIR>/logg.txt.

Hantera Spreaker-fliken kopplar ihop arkivet med avsnittslistan via
Spreakers episode_id, som läses ur .xml-filernas <guid> (se index()). "Generera
om" använder då den lokala mp3:an i stället för att ladda ner ljudet igen,
och ett sparat transkript i stället för att transkribera på nytt.

Filnamnen byggs EXAKT som i PowerShell-skriptet
("<yyyy-MM-dd_HH-mm>_<talare>_<titel>.mp3", lokal tid), så ett arkiv som
redan laddats ner med skriptet känns igen och inga avsnitt laddas ner igen.

Körningen sker i en egen bakgrundstråd (start()), helt fristående från
bearbetningskön - det är ren nätverks-/diskhantering som inte ska blockera
eller blockeras av transkribering. Framsteg läses via get_status().
"""
# Standardbiblioteket räcker för allt utom själva HTTP-anropen:
# - html: avkodar HTML-entiteter (&amp; m.fl.) i titlar och beskrivningar
# - re: reguljära uttryck för filnamn, talare och <guid>
# - threading: bakgrundstråden och låset runt den delade statusen
# - time: monotonisk klocka för att mäta hur lång tid saker tar
# - xml.etree.ElementTree: tolkar RSS-flödet och skriver avsnittens .xml
# - email.utils.parsedate_to_datetime: RSS-datum är i e-postformat (RFC 2822)
import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path

import requests

import config
from modules import spreaker_episode_store
from modules.app_logging import logger

# Showens publika RSS-flöde. Det är samma adress som podcastappar använder,
# så det kräver ingen inloggning eller API-token - bara showens id.
RSS_URL_TEMPLATE = "https://www.spreaker.com/show/{show_id}/episodes/feed"
# Samma User-Agent som PowerShell-skriptet använde, så eventuella
# serverloggar hos Spreaker ser likadana ut oavsett vilket verktyg som körts.
USER_AGENT = "PodcastDownloader/1.0"
# Maxlängder för delarna i filnamnet (samma som i skriptet). Windows har en
# gräns på 260 tecken för hela sökvägen, så långa titlar måste kortas.
MAX_TITLE = 80
MAX_SPEAKER = 40
# XML-namnrymden för iTunes-fälten (itunes:duration, itunes:keywords m.fl.),
# som behövs för att hitta dem med ElementTree.find().
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

# Filändelsen för ett sparat transkript: "<avsnitt>.transkript.txt".
TRANSCRIPT_SUFFIX = ".transkript.txt"
# Plockar ut Spreakers avsnitts-id ur <guid>, som ser ut så här:
#   <guid isPermaLink="false">https://api.spreaker.com/episode/75240488</guid>
# Ett reguljärt uttryck på råtexten räcker och är mycket snabbare än att
# tolka varje .xml-fil som XML när hela arkivet indexeras (se index()).
_GUID_EPISODE_ID = re.compile(r"<guid[^>]*>[^<]*/episode/(\d+)\s*</guid>")

# Windows otillåtna filnamnstecken (samma som [IO.Path]::GetInvalidFileNameChars()
# i skriptet). Används på alla plattformar så filnamnen blir identiska oavsett
# om arkivet skapas på Windows eller i Docker/Linux.
_INVALID_FILENAME_CHARS = set('"<>|:*?\\/') | {chr(i) for i in range(32)}


class ArchiveError(Exception):
    """Ett väntat fel i arkiveringen, med ett meddelande som kan visas för användaren."""

    pass


# ---------------------------------------------------------------- status
#
# Statusen delas mellan två trådar: bakgrundstråden som arkiverar skriver
# till den, och webbservern läser den när sidan frågar hur det går
# (GET /api/spreaker/archive/status). Därför skyddas den av ett lås, och
# läsaren får alltid en KOPIA så att den aldrig ser en halvuppdaterad lista.

_lock = threading.Lock()
# Signal till bakgrundstråden om att användaren klickat "Avbryt".
_stop_event = threading.Event()
# Bakgrundstråden för pågående (eller senaste) körning.
_thread: threading.Thread | None = None
_status: dict = {
    "running": False,  # pågår en körning just nu?
    "stopping": False,  # har användaren bett om avbrott?
    "started_at": None,  # när körningen startade (ISO-tid)
    "finished_at": None,  # när den blev klar; None medan den pågår
    "archive_dir": None,  # mappen som körningen skriver till
    "total": 0,  # antal avsnitt i flödet
    "index": 0,  # vilket avsnitt som behandlas nu (1-baserat)
    "current": "",  # titel på avsnittet som behandlas nu
    "bytes_done": 0,  # nedladdat hittills av aktuell fil
    "bytes_total": 0,  # aktuell fils storlek (0 = okänd)
    "downloaded": 0,  # antal nya mp3-filer i körningen
    "downloaded_bytes": 0,  # summa nedladdade byte i körningen
    "skipped": 0,  # antal avsnitt vars mp3 redan fanns
    "failures": [],  # "titel - fel" för avsnitt som misslyckades
    "error": None,  # fel som stoppade hela körningen
}


def get_status() -> dict:
    """
    Aktuell status som en kopia, säker att skicka vidare till webbsidan.

    Anropas av GET /api/spreaker/archive/status, som sidan pollar varje sekund
    medan en arkivering pågår.

    Returns:
        En ny dict med samma fält som _status. Ändringar i den påverkar inte
        den riktiga statusen.
    """
    with _lock:
        status = dict(_status)
        # Listan måste kopieras separat - dict() kopierar bara ytligt, och
        # bakgrundstråden kan annars ändra listan medan den skickas iväg.
        status["failures"] = list(_status["failures"])
    # Innan första körningen finns ingen mapp i statusen - visa då den
    # mapp som NÄSTA körning skulle använda.
    status["archive_dir"] = status["archive_dir"] or str(config.ARCHIVE_DIR)
    return status


def _update(**changes) -> None:
    """
    Uppdaterar ett eller flera fält i statusen under låset.

    Exempel: _update(index=3, current="Avsnittets titel")

    Args:
        **changes: Fältnamn och nya värden, samma namn som i _status.
    """
    with _lock:
        _status.update(changes)


def is_available() -> bool:
    """
    Arkivet kräver bara showens id - flödet är publikt, ingen token behövs.

    Returns:
        True om SPREAKER_SHOW_ID är ifyllt. Styr om arkivrutan visas i
        Hantera Spreaker-fliken och om arkiv-endpointsen svarar.
    """
    return bool(config.SPREAKER_SHOW_ID)


def feed_url() -> str:
    """
    Adressen till showens RSS-flöde.

    Returns:
        T.ex. "https://www.spreaker.com/show/7349463/episodes/feed".
    """
    return RSS_URL_TEMPLATE.format(show_id=config.SPREAKER_SHOW_ID)


def start() -> bool:
    """
    Startar en arkivkörning i bakgrunden. Returnerar False om en redan pågår.

    Körningen sker i en egen tråd (se _run_safely/run), så anropet returnerar
    direkt och webbsidan kan följa förloppet via get_status().

    Returns:
        True om en ny körning startades, False om en redan pågick.
    """
    global _thread
    with _lock:
        # Kontroll och start sker under samma lås, så två snabba klick på
        # "Arkivera podden" aldrig kan starta två körningar samtidigt.
        if _status["running"]:
            return False
        _stop_event.clear()
        # Nollställ allt från förra körningen så sidan inte visar gamla siffror.
        _status.update(
            running=True, stopping=False, started_at=datetime.now().isoformat(), finished_at=None,
            archive_dir=str(config.ARCHIVE_DIR), total=0, index=0, current="Hämtar flödet...",
            bytes_done=0, bytes_total=0, downloaded=0, downloaded_bytes=0, skipped=0,
            failures=[], error=None,
        )
    # daemon=True: tråden hindrar inte appen från att stängas. En avbruten
    # nedladdning lämnar bara en .part-fil, som skrivs över nästa gång.
    _thread = threading.Thread(target=_run_safely, daemon=True, name="podcast-archive")
    _thread.start()
    return True


def stop() -> None:
    """
    Ber en pågående körning att avbryta efter nuvarande avsnitt/nedladdningsbit.

    Avbrottet sker inom någon sekund: nedladdningen kontrollerar signalen
    för varje bit på 256 kB, och huvudloopen före varje nytt avsnitt.
    """
    _stop_event.set()
    _update(stopping=True)


# ---------------------------------------------------------------- hjälpfunktioner

def _format_size(num: float) -> str:
    """
    Byte som läsbar storlek, t.ex. "67.3 MB" (samma format som skriptets logg).

    Args:
        num: Antal byte.

    Returns:
        Storleken i kB, MB eller GB med lämpligt antal decimaler.
    """
    if num >= 1024**3:
        return f"{num / 1024**3:.2f} GB"
    if num >= 1024**2:
        return f"{num / 1024**2:.1f} MB"
    return f"{num / 1024:.0f} kB"


def _format_time(seconds: float) -> str:
    """
    Sekunder som läsbar tid, t.ex. "6 min 45 s". Minst 1 s, så loggen aldrig visar "0 s".

    Args:
        seconds: Tid i sekunder (decimaler avrundas nedåt).

    Returns:
        Tiden i timmar och minuter, minuter och sekunder, eller sekunder.
    """
    seconds = max(1, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} h {(seconds % 3600) // 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds} s"


def _add_log(archive_dir: Path, message: str, level: str = "INFO") -> None:
    """
    Lägger till en rad i arkivets logg.txt, i samma format som skriptet:
    "2026-09-23 06:58:43  INFO   meddelande" med Windows-radslut (CRLF), så
    loggen ser rätt ut i Anteckningar.

    Args:
        archive_dir: Arkivmappen där logg.txt ligger (skapas vid behov).
        message: Texten som ska loggas.
        level: "INFO", "VARN" eller "FEL" (samma nivåer som skriptet).
    """
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {level:<5}  {message}\r\n"
    try:
        # utf-8-sig ger BOM i en ny fil (som skriptet); vid tillägg skrivs ingen ny BOM.
        encoding = "utf-8" if (archive_dir / "logg.txt").exists() else "utf-8-sig"
        # newline="" hindrar Python från att göra om \r\n till \r\r\n på Windows.
        with open(archive_dir / "logg.txt", "a", encoding=encoding, newline="") as f:
            f.write(line)
    except OSError:
        # Loggen är en bonus - ett skrivfel där (t.ex. filen öppen i ett annat
        # program) ska aldrig stoppa själva arkiveringen.
        pass


def safe_filename(text: str, max_length: int = 0) -> str:
    """
    Gör en titel eller ett namn till en säker del av ett filnamn - exakt som
    skriptets Get-SafeFileName, så befintliga arkiv känns igen.

    Exempel: "Nåd &amp; frid: del 1." -> "Nåd & frid_ del 1"

    Args:
        text: Titel eller namn, som det står i flödet.
        max_length: Högsta längd (0 = ingen gräns).

    Returns:
        Texten utan otillåtna tecken, eller "" om den var tom.
    """
    if not text or not text.strip():
        return ""
    # "&amp;" ska bli "&" i filnamnet, inte ordet "amp".
    text = html.unescape(text)
    # Otillåtna tecken (: / ? m.fl.) ersätts med understreck.
    text = "".join("_" if ch in _INVALID_FILENAME_CHARS else ch for ch in text)
    # Hakparenteser fungerar som jokertecken i PowerShell och ställde till
    # problem i skriptet - därför blev de vanliga parenteser där, och här.
    text = text.replace("[", "(").replace("]", ")")
    # Radbrytningar och dubbla mellanslag blir ett enda mellanslag.
    text = re.sub(r"\s+", " ", text).strip()
    if max_length > 0 and len(text) > max_length:
        text = text[:max_length]
    # Windows tillåter inte att ett filnamn slutar med punkt eller mellanslag.
    return text.rstrip(". ")


def clean_description(text: str) -> str:
    """
    Gör om flödets HTML-beskrivning till läsbar ren text för .txt-filen:
    radbrytningar och stycken bevaras, alla andra taggar tas bort.

    Args:
        text: Beskrivningen ur <description> (HTML med <p>, <br> m.m.).

    Returns:
        Ren text med Windows-radslut, eller "" om beskrivningen saknas.
    """
    if not text or not text.strip():
        return ""
    # <br> blir radbrytning och </p> blir en tom rad mellan stycken...
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    # ...och alla övriga taggar tas bort helt.
    text = re.sub(r"<[^>]+>", "", text)
    # Spreaker dubbelkodar ibland tecken (&amp;quot;), så avkoda två gånger
    text = html.unescape(html.unescape(text))
    # Städa varje rad: flera mellanslag/tabbar blir ett, inga blanka i kanterna.
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in re.split(r"\r?\n", text)]
    # Högst en tom rad i följd, med Windows-radslut som i resten av filen.
    text = re.sub(r"(\r\n){3,}", "\r\n\r\n", "\r\n".join(lines))
    return text.strip()


def _format_duration(text: str) -> str:
    """
    itunes:duration (sekunder, t.ex. "4183") som "1:09:43" eller "49:12".
    Om flödet redan har en formaterad tid används den som den är.

    Args:
        text: Värdet ur <itunes:duration>.

    Returns:
        Tiden som H:MM:SS eller MM:SS, eller originaltexten om den inte är
        ett heltal.
    """
    try:
        total = int(text)
    except (TypeError, ValueError):
        return text or ""
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def split_title(title: str, description: str) -> tuple[str, str]:
    """
    Delar upp "Namn: Titel" eller "Titel - Namn" i (talare, avsnittstitel).

    Args:
        title: Avsnittets titel, t.ex. "Hans Weichbrodt: Syndens betydelse".
        description: Den rensade beskrivningen, där en rad "Talare: X" kan
            bekräfta vem som är talare.

    Returns:
        (talare, titel). Talaren är "" om den inte gick att hitta.
    """
    # Talaren står oftast på en egen rad "Talare: X" i beskrivningen (appen
    # lägger alltid dit den raden, se services/pipeline.py). Den är den
    # säkraste källan och används för att kontrollera titeln nedan.
    speaker = ""
    m = re.search(r"(?m)^Talare:[ \t]*(.+?)[ \t]*\r?$", description)
    if m:
        speaker = m.group(1).strip()

    # Vanligast: "Hans Weichbrodt: Syndens betydelse i himlen". Delen före
    # kolon räknas som talare om den är 2-60 tecken och (om beskrivningen
    # har en talarrad) stämmer med den - annars kunde en titel som
    # "Johannes 3:16" felaktigt delas upp.
    m = re.match(r"^\s*([^:]{2,60}?)\s*:\s*(.+)$", title)
    if m and (not speaker or m.group(1) == speaker):
        return m.group(1).strip(), m.group(2).strip()
    # Äldre avsnitt har ibland formen "Titel - Namn".
    if speaker and title.endswith(f" - {speaker}"):
        return speaker, title[: len(title) - len(speaker) - 3].strip()
    # Ingen känd form - hela titeln är titel.
    return speaker, title.strip()


def _parse_pub_date(text: str) -> datetime | None:
    """
    <pubDate> (e-postformat, t.ex. "Sun, 07 Sep 2025 09:30:00 +0000") som
    LOKAL tid - skriptet använde lokal tid i filnamnen, så det gör vi också.

    Args:
        text: Värdet ur <pubDate>.

    Returns:
        Datum och tid i datorns tidszon, eller None om texten inte kunde tolkas.
    """
    try:
        return parsedate_to_datetime(text).astimezone()
    except (TypeError, ValueError, IndexError):
        # Saknat eller trasigt datum - filnamnet får då "okant-datum".
        return None


def _text(item: ET.Element, path: str) -> str:
    """
    Texten i ett underelement (t.ex. "title" eller "itunes:duration"), eller "" om det saknas.

    Args:
        item: Ett <item>-element ur flödet.
        path: Elementets namn, med prefixet itunes: för iTunes-fälten.

    Returns:
        Texten utan blanktecken i kanterna. CDATA-innehåll hanteras
        automatiskt av XML-tolken.
    """
    node = item.find(path, {"itunes": ITUNES_NS})
    return (node.text or "").strip() if node is not None else ""


def episode_basename(item: ET.Element) -> tuple[str, dict]:
    """
    Bygger filnamnsbasen (utan filändelse) + de fält som behövs för .txt-filen.

    Args:
        item: Ett <item>-element ur flödet.

    Returns:
        (bas, info) där bas är t.ex.
        "2026-09-20_10-30_Hans Weichbrodt_Syndens betydelse i himlen" och info
        innehåller full_title, speaker, title, date och description.
    """
    full_title = _text(item, "title")
    # Beskrivningen används både för .txt-filen och för att hitta talaren.
    # Vissa avsnitt har bara itunes:summary, därav reservvägen.
    description = clean_description(_text(item, "description")) or clean_description(
        _text(item, "itunes:summary")
    )
    speaker, title = split_title(full_title, description)
    # Hittades ingen talare i titel/beskrivning provas itunes:author.
    speaker = speaker or _text(item, "itunes:author") or "Okänd talare"
    date = _parse_pub_date(_text(item, "pubDate"))
    # Datum först i filnamnet gör att arkivmappen sorteras i tidsordning.
    date_part = date.strftime("%Y-%m-%d_%H-%M") if date else "okant-datum"
    base = f"{date_part}_{safe_filename(speaker, MAX_SPEAKER)}_{safe_filename(title, MAX_TITLE)}"
    return base, {
        "full_title": full_title, "speaker": speaker, "title": title,
        "date": date, "description": description,
    }


def episode_id_of(item: ET.Element) -> int | None:
    """
    Spreakers episode_id ur <guid> (https://api.spreaker.com/episode/<id>).

    Args:
        item: Ett <item>-element ur flödet.

    Returns:
        Avsnittets id som heltal, eller None om <guid> har ett annat format.
    """
    m = re.search(r"/episode/(\d+)\s*$", _text(item, "guid"))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- koppling till Spreaker-avsnitt
#
# Hantera Spreaker-fliken listar avsnitt med Spreakers episode_id, men
# arkivets filer heter "<datum>_<talare>_<titel>". Kopplingen görs via
# <guid> i varje avsnitts .xml-fil, som innehåller episode_id.

# Cache för index(): nyckeln är (mapp, mappens ändringstid). Så länge inga
# filer lagts till eller tagits bort återanvänds resultatet.
_index_cache: dict = {"key": None, "index": {}}


def index() -> dict[int, Path]:
    """
    episode_id -> filnamnsbas (utan ändelse) för alla avsnitt i arkivet,
    utläst ur .xml-filernas <guid>. Cachas tills arkivmappen ändras (mappens
    mtime ändras när filer läggs till/tas bort). Saknas mappen (t.ex. en
    urkopplad extern disk) returneras en tom karta i stället för ett fel.
    """
    archive_dir = Path(config.ARCHIVE_DIR)
    try:
        key = (str(archive_dir), archive_dir.stat().st_mtime_ns)
    except OSError:
        return {}
    with _lock:
        if _index_cache["key"] == key:
            # Kopia, så anroparen inte kan ändra i cachen av misstag.
            return dict(_index_cache["index"])

    # Läs alla .xml-filer. Det tar bara någon tiondel för ett arkiv med
    # ett hundratal avsnitt, och görs bara när mappen faktiskt ändrats.
    result: dict[int, Path] = {}
    try:
        for xml_path in archive_dir.glob("*.xml"):
            try:
                # utf-8-sig hoppar över BOM:en som filerna skrivs med.
                m = _GUID_EPISODE_ID.search(xml_path.read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                # En fil som inte går att läsa (t.ex. låst) hoppas över.
                continue
            if m:
                result[int(m.group(1))] = xml_path.with_suffix("")
    except OSError:
        return {}
    with _lock:
        _index_cache.update(key=key, index=result)
    return dict(result)


def _transcript_file(base: Path) -> Path:
    """
    Sökvägen till transkriptet för en filnamnsbas.

    Args:
        base: Filnamnsbasen utan ändelse (från index()).

    Returns:
        "<bas>.transkript.txt" i samma mapp.
    """
    return base.with_name(base.name + TRANSCRIPT_SUFFIX)


def _audio_file(base: Path) -> Path:
    """
    Sökvägen till mp3:an för en filnamnsbas.

    Args:
        base: Filnamnsbasen utan ändelse (från index()).

    Returns:
        "<bas>.mp3" i samma mapp.
    """
    # with_name i stället för with_suffix: titlar kan innehålla punkter,
    # och with_suffix skulle då ersätta allt efter sista punkten i titeln.
    return base.with_name(base.name + ".mp3")


def local_info(episode_id: int, idx: dict[int, Path] | None = None) -> dict:
    """
    {"archived": finns mp3 lokalt, "has_transcript": finns sparat transkript} för ett avsnitt.

    Args:
        episode_id: Spreakers id för avsnittet.
        idx: En redan hämtad index() - skickas med när många avsnitt slås
            upp i rad, så arkivet inte läses om för varje avsnitt.

    Returns:
        En dict med två booleska värden, som visas som 🗄️ och 📝 i listan.
    """
    # idx kan skickas med när många avsnitt slås upp i rad (hela listan i
    # Hantera Spreaker), så index() inte anropas en gång per avsnitt.
    base = (idx if idx is not None else index()).get(episode_id)
    if base is None:
        return {"archived": False, "has_transcript": False}
    audio = _audio_file(base)
    return {
        # En tom mp3 (avbruten nedladdning) räknas inte som arkiverad.
        "archived": audio.exists() and audio.stat().st_size > 0,
        "has_transcript": _transcript_file(base).exists(),
    }


def audio_path(episode_id: int) -> Path | None:
    """
    Den arkiverade mp3:an för ett avsnitt, om den finns.

    Args:
        episode_id: Spreakers id för avsnittet.

    Returns:
        Sökvägen till mp3:an, eller None om avsnittet inte är arkiverat
        (eller filen är tom).
    """
    base = index().get(episode_id)
    if base is None:
        return None
    audio = _audio_file(base)
    try:
        return audio if audio.stat().st_size > 0 else None
    except OSError:
        # Filen fanns i index men är borta nu (t.ex. raderad för hand).
        return None


def read_transcript(episode_id: int) -> str | None:
    """
    Ett sparat transkript för avsnittet, eller None om inget finns.

    Args:
        episode_id: Spreakers id för avsnittet.

    Returns:
        Transkriptets text, eller None om avsnittet inte är arkiverat, saknar
        transkript eller om filen är tom.
    """
    base = index().get(episode_id)
    if base is None:
        return None
    try:
        text = _transcript_file(base).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    # En tom fil räknas som inget transkript.
    return text or None


def save_transcript(episode_id: int, transcript: str) -> bool:
    """
    Sparar hela transkriberingen bredvid avsnittets mp3 i arkivet. Gör
    ingenting (returnerar False) om avsnittet inte finns i arkivet - det
    ska inte skapa lösa filer utan tillhörande mp3/xml/txt.

    Args:
        episode_id: Spreakers id för avsnittet.
        transcript: Hela transkriptet.

    Returns:
        True om filen skrevs, annars False.
    """
    base = index().get(episode_id)
    if base is None or not transcript.strip():
        return False
    # utf-8-sig (med BOM) så att å/ä/ö visas rätt även i äldre Windows-program.
    _transcript_file(base).write_text(transcript.strip() + "\n", encoding="utf-8-sig")
    return True


# ---------------------------------------------------------------- flöde & filer

def _fetch_feed() -> tuple[bytes, ET.Element]:
    """
    Hämtar och tolkar RSS-flödet. Returnerar både råbytes och XML-roten.

    Returns:
        (råbytes, rotelement). Råbytes behövs för _register_namespaces.

    Raises:
        ArchiveError: Om servern inte svarar 200 eller flödet inte är giltig XML.
    """
    response = requests.get(feed_url(), headers={"User-Agent": USER_AGENT}, timeout=60)
    if response.status_code != 200:
        raise ArchiveError(f"Kunde inte hämta RSS-flödet ({response.status_code}).")
    try:
        # .content (bytes), inte .text: XML-tolken läser själv teckenkodningen
        # ur <?xml ... encoding="UTF-8"?>, så å/ä/ö alltid blir rätt.
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise ArchiveError(f"RSS-flödet gick inte att tolka: {exc}") from exc
    # Råbytes behövs också, för att läsa ut namnrymdsprefixen (se nedan).
    return response.content, root


def _register_namespaces(feed_bytes: bytes) -> None:
    """
    Behåller flödets egna prefix (itunes:, googleplay: ...) när avsnitts-XML skrivs ut.

    Args:
        feed_bytes: Hela flödet som bytes.
    """
    # Utan detta skriver ElementTree ut "ns0:duration" i stället för
    # "itunes:duration" - giltigt XML, men svårläst och olikt originalet.
    for _event, (prefix, uri) in ET.iterparse(BytesIO(feed_bytes), events=("start-ns",)):
        if prefix:
            ET.register_namespace(prefix, uri)


def _save_episode_xml(root: ET.Element, item: ET.Element, path: Path) -> None:
    """
    Sparar avsnittets <item> som ett eget litet RSS-dokument (med kanalens titel/länk).

    Args:
        root: Flödets rotelement (<rss>).
        item: Avsnittets <item>.
        path: Filen som ska skrivas (<bas>.xml).
    """
    # Samma rotelement (<rss> med samma attribut) som i originalflödet...
    new_root = ET.Element(root.tag, dict(root.attrib))
    channel = ET.SubElement(new_root, "channel")
    # ...och kanalens titel/länk, så filen säger vilken podd avsnittet hör till.
    for tag in ("title", "link"):
        node = root.find(f"channel/{tag}")
        if node is not None:
            channel.append(node)
    channel.append(item)
    # Indrag gör filen läsbar i en vanlig texteditor.
    ET.indent(new_root)
    tree = ET.ElementTree(new_root)
    with open(path, "wb") as f:
        # BOM först (som skriptet), sedan XML-deklarationen och innehållet.
        f.write(b"\xef\xbb\xbf")
        tree.write(f, encoding="utf-8", xml_declaration=True)


def _write_txt(item: ET.Element, info: dict, url: str, path: Path) -> None:
    """
    Skriver den läsbara sammanställningen (.txt) för ett avsnitt, i skriptets format.

    Args:
        item: Avsnittets <item> (för längd, nyckelord, länk och guid).
        info: Titel, talare, datum och beskrivning från episode_basename().
        url: Ljudfilens adress.
        path: Filen som ska skrivas (<bas>.txt).
    """
    date = info["date"]
    # "a,b ,c" -> "a, b, c"
    keywords = re.sub(r"\s*,\s*", ", ", _text(item, "itunes:keywords"))
    # Etiketterna har samma bredd så värdena hamnar i en rak kolumn.
    lines = [
        f"Titel:       {info['full_title']}",
        f"Talare:      {info['speaker']}",
        f"Datum:       {date.strftime('%Y-%m-%d %H:%M') if date else _text(item, 'pubDate')}",
        f"Längd:       {_format_duration(_text(item, 'itunes:duration'))}",
        f"Nyckelord:   {keywords}",
        f"Länk:        {_text(item, 'link')}",
        f"Avsnitts-ID: {_text(item, 'guid')}",
        f"MP3-källa:   {url}",
        "",
        "----------------------------------------------------------------",
        "",
        info["description"],
    ]
    # CRLF och BOM, så filen öppnas rätt i Anteckningar på Windows.
    path.write_text("\r\n".join(lines), encoding="utf-8-sig", newline="")


def _download(url: str, dest: Path, expected: int) -> int:
    """
    Strömmar ner till <dest>.part och döper om när allt kommit. Returnerar antal byte.

    Args:
        url: Ljudfilens adress (<enclosure url>).
        dest: Slutlig sökväg för mp3:an.
        expected: Storlek enligt flödet, används om servern inte anger någon.

    Returns:
        Antal nedladdade byte.

    Raises:
        ArchiveError: Vid felsvar, avbrott från användaren eller ofullständig fil.
    """
    # Nedladdningen skrivs först till en .part-fil. Bara en KOMPLETT fil får
    # det riktiga namnet - annars skulle en avbruten nedladdning se ut som
    # en färdig mp3 och hoppas över vid nästa körning.
    temp = dest.with_name(dest.name + ".part")
    try:
        # timeout=(30, 60): högst 30 s för att ansluta och 60 s utan data.
        # Själva nedladdningen får ta hur lång tid som helst.
        with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=(30, 60)) as resp:
            if resp.status_code != 200:
                raise ArchiveError(f"Servern svarade {resp.status_code}")
            # Storleken från servern, annars den som flödet angav (<enclosure length>).
            total = int(resp.headers.get("Content-Length") or 0) or expected
            _update(bytes_done=0, bytes_total=total)
            done = 0
            with open(temp, "wb") as f:
                # 256 kB åt gången: stort nog för bra fart, litet nog för att
                # "Avbryt" och framstegsmätaren ska svara snabbt.
                for chunk in resp.iter_content(chunk_size=262144):
                    if _stop_event.is_set():
                        raise ArchiveError("Avbruten")
                    f.write(chunk)
                    done += len(chunk)
                    _update(bytes_done=done)
        # Tappad anslutning kan se ut som ett normalt slut - jämför med
        # förväntad storlek så att en halv fil inte sparas som hel.
        if total and done < total:
            raise ArchiveError(f"Nedladdningen avbröts ({_format_size(done)} av {_format_size(total)})")
        # replace() skriver över en ev. gammal fil med samma namn.
        temp.replace(dest)
        return done
    except BaseException:
        # BaseException fångar även Ctrl+C - .part-filen städas alltid bort.
        temp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- körning

def _run_safely() -> None:
    """
    Bakgrundstrådens startpunkt: kör run() och ser till att statusen alltid avslutas.

    Utan den här ramen skulle ett oväntat fel få tråden att dö tyst, och
    sidan skulle visa "pågår" för evigt.
    """
    try:
        run()
    except Exception as exc:  # oväntat fel - visa det i UI:t i stället för att tråden dör tyst
        logger.exception("Podd-arkivet misslyckades")
        _update(error=str(exc))
    finally:
        # Oavsett hur körningen slutade ska sidan se att den är klar.
        _update(running=False, stopping=False, current="", finished_at=datetime.now().isoformat())


def run() -> None:
    """
    En hel arkivkörning: hämta flödet, gå igenom varje avsnitt och spara
    det som saknas. Körs normalt i bakgrunden via start(), men kan också
    anropas direkt (som testerna gör).

    Raises:
        ArchiveError: Om arkivmappen inte kan skapas eller flödet inte kan
            hämtas. Fel på enskilda avsnitt samlas i statusen i stället.
    """
    archive_dir = Path(config.ARCHIVE_DIR)
    try:
        # Mappen skapas först här, inte när appen startar - så appen kan
        # starta även om arkivdisken är urkopplad.
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(
            f"Arkivmappen {archive_dir} kunde inte skapas ({exc}). "
            "Är disken ansluten? Mappen ställs in med ARCHIVE_DIR i .env."
        ) from exc

    started = time.monotonic()
    _add_log(archive_dir, "==== Körning startad ====")
    try:
        feed_bytes, root = _fetch_feed()
    except (requests.RequestException, ArchiveError) as exc:
        # Utan flöde finns inget att göra - avbryt hela körningen.
        _add_log(archive_dir, f"Kunde inte hämta flödet: {exc}", "FEL")
        raise ArchiveError(f"Kunde inte hämta flödet: {exc}") from exc
    _register_namespaces(feed_bytes)

    items = root.findall("channel/item")
    show = (root.findtext("channel/title") or "").strip()
    _add_log(archive_dir, f"Flöde hämtat: {show}, {len(items)} avsnitt")
    _update(total=len(items))
    logger.info(f"Podd-arkiv: {len(items)} avsnitt i flödet, sparar i {archive_dir}")

    downloaded = downloaded_bytes = skipped = 0
    failures: list[str] = []
    # Flödet listar nyaste avsnittet först, så nya avsnitt hämtas först.
    for index, item in enumerate(items, start=1):
        if _stop_event.is_set():
            _add_log(archive_dir, "Avbruten av användaren", "VARN")
            break
        full_title = _text(item, "title")
        _update(index=index, current=full_title, bytes_done=0, bytes_total=0)
        # Varje avsnitt har en egen try: ett fel på ETT avsnitt (t.ex. en
        # ljudfil som tagits bort hos Spreaker) ska inte stoppa resten.
        try:
            # <enclosure url="..." length="..."> pekar på ljudfilen.
            enclosure = item.find("enclosure")
            url = (enclosure.get("url") or "").strip() if enclosure is not None else ""
            if not url:
                _add_log(archive_dir, f"Ingen ljudfil: {full_title}", "VARN")
                continue
            try:
                expected = int(enclosure.get("length") or 0)
            except ValueError:
                expected = 0

            base, info = episode_basename(item)
            mp3_path = archive_dir / f"{base}.mp3"

            # En redan nedladdad mp3 hämtas aldrig igen - det är det som gör
            # att en ny körning bara tar några sekunder.
            if mp3_path.exists() and mp3_path.stat().st_size > 0:
                skipped += 1
            else:
                t0 = time.monotonic()
                size = _download(url, mp3_path, expected)
                elapsed = time.monotonic() - t0
                _add_log(archive_dir, f"Nedladdad: {base}.mp3 ({_format_size(size)}, {_format_time(elapsed)})")
                downloaded += 1
                downloaded_bytes += size

            # .xml och .txt skrivs ALLTID om, så att ändrade titlar och
            # beskrivningar på Spreaker följer med till arkivet.
            _save_episode_xml(root, item, archive_dir / f"{base}.xml")
            _write_txt(item, info, url, archive_dir / f"{base}.txt")

            # Ett avsnitt som redan transkriberats (cachat i databasen via
            # "Generera om") får sitt transkript med i arkivet också.
            episode_id = episode_id_of(item)
            transcript_path = archive_dir / f"{base}{TRANSCRIPT_SUFFIX}"
            if episode_id and not transcript_path.exists():
                transcript = spreaker_episode_store.get_transcript(episode_id)
                if transcript:
                    transcript_path.write_text(transcript.strip() + "\n", encoding="utf-8-sig")
        except Exception as exc:
            # Ett avbrott mitt i en nedladdning kastar ArchiveError("Avbruten")
            # - det ska inte räknas som ett misslyckat avsnitt.
            if _stop_event.is_set():
                _add_log(archive_dir, "Avbruten av användaren", "VARN")
                break
            _add_log(archive_dir, f"Misslyckades: {full_title} - {exc}", "FEL")
            logger.warning(f"Podd-arkiv: {full_title} misslyckades: {exc}")
            failures.append(f"{full_title} - {exc}")
        finally:
            # Räknarna uppdateras efter varje avsnitt, lyckat eller inte, så
            # sidan hela tiden visar aktuella siffror.
            _update(downloaded=downloaded, downloaded_bytes=downloaded_bytes, skipped=skipped, failures=list(failures))

    # Sammanfattning sist i loggen, samma rad som skriptet skrev.
    dl = f" ({_format_size(downloaded_bytes)})" if downloaded else ""
    _add_log(
        archive_dir,
        f"Klart på {_format_time(time.monotonic() - started)}. Nedladdade: {downloaded}{dl}, "
        f"fanns redan: {skipped}, misslyckade: {len(failures)}",
    )
