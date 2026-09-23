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

En logg över varje körning sparas i <ARCHIVE_DIR>/logg.txt.

Filnamnen byggs EXAKT som i PowerShell-skriptet
("<yyyy-MM-dd_HH-mm>_<talare>_<titel>.mp3", lokal tid), så ett arkiv som
redan laddats ner med skriptet känns igen och inga avsnitt laddas ner igen.

Körningen sker i en egen bakgrundstråd (start()), helt fristående från
bearbetningskön - det är ren nätverks-/diskhantering som inte ska blockera
eller blockeras av transkribering. Framsteg läses via get_status().
"""
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
from modules.app_logging import logger

RSS_URL_TEMPLATE = "https://www.spreaker.com/show/{show_id}/episodes/feed"
USER_AGENT = "PodcastDownloader/1.0"
MAX_TITLE = 80
MAX_SPEAKER = 40
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

# Windows otillåtna filnamnstecken (samma som [IO.Path]::GetInvalidFileNameChars()
# i skriptet). Används på alla plattformar så filnamnen blir identiska oavsett
# om arkivet skapas på Windows eller i Docker/Linux.
_INVALID_FILENAME_CHARS = set('"<>|:*?\\/') | {chr(i) for i in range(32)}


class ArchiveError(Exception):
    pass


# ---------------------------------------------------------------- status

_lock = threading.Lock()
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_status: dict = {
    "running": False,
    "stopping": False,
    "started_at": None,
    "finished_at": None,
    "archive_dir": None,
    "total": 0,
    "index": 0,
    "current": "",
    "bytes_done": 0,
    "bytes_total": 0,
    "downloaded": 0,
    "downloaded_bytes": 0,
    "skipped": 0,
    "failures": [],
    "error": None,
}


def get_status() -> dict:
    with _lock:
        status = dict(_status)
        status["failures"] = list(_status["failures"])
    status["archive_dir"] = status["archive_dir"] or str(config.ARCHIVE_DIR)
    return status


def _update(**changes) -> None:
    with _lock:
        _status.update(changes)


def is_available() -> bool:
    return bool(config.SPREAKER_SHOW_ID)


def feed_url() -> str:
    return RSS_URL_TEMPLATE.format(show_id=config.SPREAKER_SHOW_ID)


def start() -> bool:
    """Startar en arkivkörning i bakgrunden. Returnerar False om en redan pågår."""
    global _thread
    with _lock:
        if _status["running"]:
            return False
        _stop_event.clear()
        _status.update(
            running=True, stopping=False, started_at=datetime.now().isoformat(), finished_at=None,
            archive_dir=str(config.ARCHIVE_DIR), total=0, index=0, current="Hämtar flödet...",
            bytes_done=0, bytes_total=0, downloaded=0, downloaded_bytes=0, skipped=0,
            failures=[], error=None,
        )
    _thread = threading.Thread(target=_run_safely, daemon=True, name="podcast-archive")
    _thread.start()
    return True


def stop() -> None:
    """Ber en pågående körning att avbryta efter nuvarande avsnitt/nedladdningsbit."""
    _stop_event.set()
    _update(stopping=True)


# ---------------------------------------------------------------- hjälpfunktioner

def _format_size(num: float) -> str:
    if num >= 1024**3:
        return f"{num / 1024**3:.2f} GB"
    if num >= 1024**2:
        return f"{num / 1024**2:.1f} MB"
    return f"{num / 1024:.0f} kB"


def _format_time(seconds: float) -> str:
    seconds = max(1, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} h {(seconds % 3600) // 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds} s"


def _add_log(archive_dir: Path, message: str, level: str = "INFO") -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {level:<5}  {message}\r\n"
    try:
        # utf-8-sig ger BOM i en ny fil (som skriptet); vid tillägg skrivs ingen ny BOM.
        encoding = "utf-8" if (archive_dir / "logg.txt").exists() else "utf-8-sig"
        with open(archive_dir / "logg.txt", "a", encoding=encoding, newline="") as f:
            f.write(line)
    except OSError:
        pass


def safe_filename(text: str, max_length: int = 0) -> str:
    if not text or not text.strip():
        return ""
    text = html.unescape(text)
    text = "".join("_" if ch in _INVALID_FILENAME_CHARS else ch for ch in text)
    text = text.replace("[", "(").replace("]", ")")
    text = re.sub(r"\s+", " ", text).strip()
    if max_length > 0 and len(text) > max_length:
        text = text[:max_length]
    return text.rstrip(". ")


def clean_description(text: str) -> str:
    if not text or not text.strip():
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    # Spreaker dubbelkodar ibland tecken (&amp;quot;), så avkoda två gånger
    text = html.unescape(html.unescape(text))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in re.split(r"\r?\n", text)]
    text = re.sub(r"(\r\n){3,}", "\r\n\r\n", "\r\n".join(lines))
    return text.strip()


def _format_duration(text: str) -> str:
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
    """Delar upp "Namn: Titel" eller "Titel - Namn" i (talare, avsnittstitel)."""
    speaker = ""
    m = re.search(r"(?m)^Talare:[ \t]*(.+?)[ \t]*\r?$", description)
    if m:
        speaker = m.group(1).strip()

    m = re.match(r"^\s*([^:]{2,60}?)\s*:\s*(.+)$", title)
    if m and (not speaker or m.group(1) == speaker):
        return m.group(1).strip(), m.group(2).strip()
    if speaker and title.endswith(f" - {speaker}"):
        return speaker, title[: len(title) - len(speaker) - 3].strip()
    return speaker, title.strip()


def _parse_pub_date(text: str) -> datetime | None:
    try:
        return parsedate_to_datetime(text).astimezone()
    except (TypeError, ValueError, IndexError):
        return None


def _text(item: ET.Element, path: str) -> str:
    node = item.find(path, {"itunes": ITUNES_NS})
    return (node.text or "").strip() if node is not None else ""


def episode_basename(item: ET.Element) -> tuple[str, dict]:
    """Bygger filnamnsbasen (utan filändelse) + de fält som behövs för .txt-filen."""
    full_title = _text(item, "title")
    description = clean_description(_text(item, "description")) or clean_description(
        _text(item, "itunes:summary")
    )
    speaker, title = split_title(full_title, description)
    speaker = speaker or _text(item, "itunes:author") or "Okänd talare"
    date = _parse_pub_date(_text(item, "pubDate"))
    date_part = date.strftime("%Y-%m-%d_%H-%M") if date else "okant-datum"
    base = f"{date_part}_{safe_filename(speaker, MAX_SPEAKER)}_{safe_filename(title, MAX_TITLE)}"
    return base, {
        "full_title": full_title, "speaker": speaker, "title": title,
        "date": date, "description": description,
    }


# ---------------------------------------------------------------- flöde & filer

def _fetch_feed() -> tuple[bytes, ET.Element]:
    response = requests.get(feed_url(), headers={"User-Agent": USER_AGENT}, timeout=60)
    if response.status_code != 200:
        raise ArchiveError(f"Kunde inte hämta RSS-flödet ({response.status_code}).")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise ArchiveError(f"RSS-flödet gick inte att tolka: {exc}") from exc
    return response.content, root


def _register_namespaces(feed_bytes: bytes) -> None:
    """Behåller flödets egna prefix (itunes:, googleplay: ...) när avsnitts-XML skrivs ut."""
    for _event, (prefix, uri) in ET.iterparse(BytesIO(feed_bytes), events=("start-ns",)):
        if prefix:
            ET.register_namespace(prefix, uri)


def _save_episode_xml(root: ET.Element, item: ET.Element, path: Path) -> None:
    """Sparar avsnittets <item> som ett eget litet RSS-dokument (med kanalens titel/länk)."""
    new_root = ET.Element(root.tag, dict(root.attrib))
    channel = ET.SubElement(new_root, "channel")
    for tag in ("title", "link"):
        node = root.find(f"channel/{tag}")
        if node is not None:
            channel.append(node)
    channel.append(item)
    ET.indent(new_root)
    tree = ET.ElementTree(new_root)
    with open(path, "wb") as f:
        f.write(b"\xef\xbb\xbf")
        tree.write(f, encoding="utf-8", xml_declaration=True)


def _write_txt(item: ET.Element, info: dict, url: str, path: Path) -> None:
    date = info["date"]
    keywords = re.sub(r"\s*,\s*", ", ", _text(item, "itunes:keywords"))
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
    path.write_text("\r\n".join(lines), encoding="utf-8-sig", newline="")


def _download(url: str, dest: Path, expected: int) -> int:
    """Strömmar ner till <dest>.part och döper om när allt kommit. Returnerar antal byte."""
    temp = dest.with_name(dest.name + ".part")
    try:
        with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=(30, 60)) as resp:
            if resp.status_code != 200:
                raise ArchiveError(f"Servern svarade {resp.status_code}")
            total = int(resp.headers.get("Content-Length") or 0) or expected
            _update(bytes_done=0, bytes_total=total)
            done = 0
            with open(temp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=262144):
                    if _stop_event.is_set():
                        raise ArchiveError("Avbruten")
                    f.write(chunk)
                    done += len(chunk)
                    _update(bytes_done=done)
        if total and done < total:
            raise ArchiveError(f"Nedladdningen avbröts ({_format_size(done)} av {_format_size(total)})")
        temp.replace(dest)
        return done
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- körning

def _run_safely() -> None:
    try:
        run()
    except Exception as exc:  # oväntat fel - visa det i UI:t i stället för att tråden dör tyst
        logger.exception("Podd-arkivet misslyckades")
        _update(error=str(exc))
    finally:
        _update(running=False, stopping=False, current="", finished_at=datetime.now().isoformat())


def run() -> None:
    archive_dir = Path(config.ARCHIVE_DIR)
    try:
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
    for index, item in enumerate(items, start=1):
        if _stop_event.is_set():
            _add_log(archive_dir, "Avbruten av användaren", "VARN")
            break
        full_title = _text(item, "title")
        _update(index=index, current=full_title, bytes_done=0, bytes_total=0)
        try:
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

            if mp3_path.exists() and mp3_path.stat().st_size > 0:
                skipped += 1
            else:
                t0 = time.monotonic()
                size = _download(url, mp3_path, expected)
                elapsed = time.monotonic() - t0
                _add_log(archive_dir, f"Nedladdad: {base}.mp3 ({_format_size(size)}, {_format_time(elapsed)})")
                downloaded += 1
                downloaded_bytes += size

            _save_episode_xml(root, item, archive_dir / f"{base}.xml")
            _write_txt(item, info, url, archive_dir / f"{base}.txt")
        except Exception as exc:
            if _stop_event.is_set():
                _add_log(archive_dir, "Avbruten av användaren", "VARN")
                break
            _add_log(archive_dir, f"Misslyckades: {full_title} - {exc}", "FEL")
            logger.warning(f"Podd-arkiv: {full_title} misslyckades: {exc}")
            failures.append(f"{full_title} - {exc}")
        finally:
            _update(downloaded=downloaded, downloaded_bytes=downloaded_bytes, skipped=skipped, failures=list(failures))

    dl = f" ({_format_size(downloaded_bytes)})" if downloaded else ""
    _add_log(
        archive_dir,
        f"Klart på {_format_time(time.monotonic() - started)}. Nedladdade: {downloaded}{dl}, "
        f"fanns redan: {skipped}, misslyckade: {len(failures)}",
    )
