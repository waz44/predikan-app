"""
Modul: spreaker_client
Laddar upp ett färdigt avsnitt till Spreaker via deras publika API:
https://developers.spreaker.com/guides/upload-an-episode/
https://developers.spreaker.com/guides/working-with-draft-episodes/

Om SPREAKER_SIMULATE=true (eller om token/show-id saknas) simuleras
uppladdningen istället, så att hela flödet kan testas utan riktiga
API-nycklar.

Ett enda `publish_date`-fält täcker två olika användningsfall, beroende på
om datumet ligger i framtiden eller ej:

- FRAMTIDA datum -> schemaläggning via `auto_published_at`. Spreaker kräver
  att detta fält alltid är i framtiden (UTC) - annars publiceras avsnittet
  direkt istället för att schemaläggas.
- DAGENS datum eller ETT DATUM BAKÅT I TIDEN -> avsnittet laddas upp och
  publiceras direkt som vanligt, men följs sedan av ett andra API-anrop som
  redigerar avsnittets `published_at`-fält till exakt det angivna datumet.
  Detta är den officiellt dokumenterade vägen för att bakåtdatera ett
  avsnitt (t.ex. en gammal predikan som spelades in för länge sedan) - se
  "Manual Publishing" i guiden om Draft Episodes.

En `progress_callback` kan anges och anropas löpande med verklig
uppladdningsprocent (0-100) medan filen skickas till Spreaker.
"""
from pathlib import Path
from typing import Callable, Optional
from datetime import datetime, timezone, timedelta
import time
import uuid
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
import config


class SpreakerUploadError(Exception):
    pass


def _format_publish_date(publish_date: str) -> tuple[str, datetime]:
    """
    Konverterar värdet från ett <input type="datetime-local"> ("YYYY-MM-DDTHH:MM")
    till det format Spreakers API kräver ("YYYY-MM-DD HH:MM:SS").

    VIKTIGT: Spreakers API tolkar alltid datum/tid-fält som UTC. Värdet från
    formuläret är däremot lokal tid på den här datorn. Vi antar att datorns
    inställda tidszon är samma som användarens (rimligt för en lokal
    enanvändar-app) och konverterar därför uttryckligen till UTC innan vi
    skickar det vidare.

    Returns:
        (formaterad UTC-sträng, UTC-datetime) - den senare används för att
        avgöra om datumet ligger i framtiden (schemaläggning) eller inte
        (bakåtdatering).
    """
    naive_local = datetime.fromisoformat(publish_date)
    aware_local = naive_local.astimezone()  # tolkar som datorns lokala tidszon
    utc_dt = aware_local.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%d %H:%M:%S"), utc_dt


def publish_episode(
    audio_path: Path,
    title: str,
    description: str,
    tags: list[str],
    publish_date: str = "",
    progress_callback: Optional[Callable[[int], None]] = None,
) -> dict:
    """
    Publicerar ett avsnitt på Spreaker.

    Args:
        publish_date: Valfritt, "YYYY-MM-DDTHH:MM". Framtida datum schemalägger,
                      dagens datum eller ett datum bakåt i tiden bakåtdaterar.
                      Tomt = publiceras direkt med dagens datum.
        progress_callback: Valfri funktion som anropas med uppladdningsprocent (0-100).

    Returns:
        dict med bl.a. "episode_url", "scheduled" och "backdated".
    """
    simulate = (
        config.SPREAKER_SIMULATE
        or not config.SPREAKER_API_TOKEN
        or not config.SPREAKER_SHOW_ID
    )

    auto_published_at = None  # sätts bara om vi faktiskt ska schemalägga
    backdate_utc: Optional[str] = None
    scheduled = False
    backdated = False

    if publish_date:
        try:
            formatted_utc, utc_dt = _format_publish_date(publish_date)
        except ValueError as exc:
            raise SpreakerUploadError(f"Ogiltigt publiceringsdatum: {exc}")

        now_utc = datetime.now(timezone.utc)
        if utc_dt > now_utc + timedelta(minutes=2):
            # Tillräckligt långt fram i tiden -> riktig schemaläggning
            auto_published_at = formatted_utc
            scheduled = True
        else:
            # Nu eller bakåt i tiden -> publicera direkt, bakåtdatera efteråt
            backdate_utc = formatted_utc
            backdated = True

    if simulate:
        return _simulate_publish(title, scheduled, backdated, progress_callback)

    url = config.SPREAKER_UPLOAD_URL.format(show_id=config.SPREAKER_SHOW_ID)
    headers = {"Authorization": f"Bearer {config.SPREAKER_API_TOKEN}"}

    fields = {
        "title": title,
        # OBS: "description" är dokumenterat som ett RENT TEXT-fält hos
        # Spreaker - eventuella HTML-taggar (t.ex. <br>) skickade hit
        # stryks bort av Spreaker själva. Spreaker genererar automatiskt
        # ett separat, skrivskyddat fält "description_html" (radbrytningar
        # -> <br />) utifrån denna text - se modules/text_formatting.py
        # för bakgrunden till varför det INTE görs någon HTML-konvertering
        # här (verifierat mot ett riktigt konto: taggar vi skickar in
        # stryks tyst bort, ingen effekt).
        "description": description,
        "tags": ",".join(tags) if tags else "",
    }
    if auto_published_at:
        fields["auto_published_at"] = auto_published_at
    # Om varken auto_published_at eller published_at anges publicerar
    # Spreaker automatiskt direkt med dagens datum - precis det vi vill
    # ha som utgångsläge inför en ev. bakåtdatering i efterhand.

    with open(audio_path, "rb") as audio_file:
        fields["media_file"] = (audio_path.name, audio_file, "audio/mpeg")
        encoder = MultipartEncoder(fields=fields)

        def _on_progress(monitor: MultipartEncoderMonitor) -> None:
            if progress_callback and monitor.len:
                percent = int(monitor.bytes_read / monitor.len * 100)
                # Håll den på max 99% tills vi faktiskt fått ett svar från Spreaker.
                # Om vi ska bakåtdatera efteråt, lämna lite marginal (max 90%)
                # så det uppföljande anropet också syns som "pågår" i UI:t.
                cap = 90 if backdate_utc else 99
                progress_callback(min(percent, cap))

        monitor = MultipartEncoderMonitor(encoder, _on_progress)
        headers["Content-Type"] = monitor.content_type

        response = requests.post(url, headers=headers, data=monitor, timeout=600)

    if response.status_code not in (200, 201):
        raise SpreakerUploadError(
            f"Spreaker-uppladdning misslyckades ({response.status_code}): {response.text}"
        )

    payload = response.json().get("response", {}).get("episode", {})
    episode_id = payload.get("episode_id")
    episode_url = payload.get("site_url") or f"https://www.spreaker.com/episode/{episode_id}"

    # --- Bakåtdatering: uppföljande anrop som sätter published_at ---
    if backdate_utc and episode_id:
        edit_response = requests.post(
            f"https://api.spreaker.com/v2/episodes/{episode_id}",
            headers={"Authorization": f"Bearer {config.SPREAKER_API_TOKEN}"},
            data={"published_at": backdate_utc},
            timeout=60,
        )
        if edit_response.status_code not in (200, 201):
            raise SpreakerUploadError(
                f"Avsnittet laddades upp men kunde inte bakåtdateras "
                f"({edit_response.status_code}): {edit_response.text}"
            )

    if progress_callback:
        progress_callback(100)

    return {
        "episode_id": episode_id,
        "episode_url": episode_url,
        "simulated": False,
        "scheduled": scheduled,
        "backdated": backdated,
    }


def list_episodes() -> list[dict]:
    """
    Hämtar ALLA avsnitt på det konfigurerade Spreaker-kontot, för den lokala
    hanteringscachen (se modules/spreaker_episode_store.py). Anropar ALLTID
    det riktiga API:t - ignorerar config.SPREAKER_SIMULATE helt, till
    skillnad från publish_episode(). Den flaggan gäller bara
    nypubliceringsflödet; den här funktionen är istället skyddad på
    router-nivå (routers/spreaker_episodes.py exponerar den bara när
    token/show-id finns OCH SIMULATE är av).

    VIKTIGT: listnings-svaret (GET .../episodes, paginerat via
    response.next_url) innehåller INTE description eller plays_count -
    bara episode_id/title/duration/published_at/site_url (bekräftat i
    praktiken - Talare/beskrivning saknades helt i hanteringstabellen tills
    detta åtgärdades). De fälten finns bara i svaret från GET på ETT
    avsnitt i taget (se get_episode) - därför görs ett extra anrop per
    avsnitt här. Det är bara en explicit "Hämta från Spreaker"-åtgärd, inte
    något som körs vid varje sidvisning, så den extra anropsvolymen är
    ett rimligt pris för att Talare-kolumnen faktiskt ska gå att visa.
    """
    url = config.SPREAKER_UPLOAD_URL.format(show_id=config.SPREAKER_SHOW_ID)
    headers = {"Authorization": f"Bearer {config.SPREAKER_API_TOKEN}"}

    episode_ids: list[int] = []
    while url:
        response = requests.get(url, headers=headers, timeout=60)
        if response.status_code != 200:
            raise SpreakerUploadError(
                f"Kunde inte hämta avsnittslistan från Spreaker ({response.status_code}): {response.text}"
            )
        payload = response.json().get("response", {})
        episode_ids.extend(item["episode_id"] for item in payload.get("items", []))
        url = payload.get("next_url")

    return [get_episode(episode_id) for episode_id in episode_ids]


def get_episode(episode_id: int) -> dict:
    """Hämtar FULLSTÄNDIGA fält för ETT avsnitt, inklusive description/plays_count (se list_episodes)."""
    response = requests.get(
        f"https://api.spreaker.com/v2/episodes/{episode_id}",
        headers={"Authorization": f"Bearer {config.SPREAKER_API_TOKEN}"},
        timeout=60,
    )
    if response.status_code != 200:
        raise SpreakerUploadError(
            f"Kunde inte hämta avsnitt {episode_id} från Spreaker ({response.status_code}): {response.text}"
        )
    return response.json().get("response", {}).get("episode", {})


def update_episode(episode_id: int, title: str, description: str) -> None:
    """Redigerar titel/beskrivning för ett REDAN publicerat avsnitt på Spreaker."""
    response = requests.post(
        f"https://api.spreaker.com/v2/episodes/{episode_id}",
        headers={"Authorization": f"Bearer {config.SPREAKER_API_TOKEN}"},
        data={"title": title, "description": description},
        timeout=60,
    )
    if response.status_code not in (200, 201):
        raise SpreakerUploadError(
            f"Kunde inte spara ändringar för avsnitt {episode_id} ({response.status_code}): {response.text}"
        )


def _simulate_publish(
    title: str,
    scheduled: bool,
    backdated: bool,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> dict:
    """Simulerar en Spreaker-publicering (ingen internetanslutning krävs)."""
    steps = [10, 30, 55, 80, 99]
    for percent in steps:
        if progress_callback:
            progress_callback(percent)
        time.sleep(0.15)

    if progress_callback:
        progress_callback(100)

    fake_id = str(uuid.uuid4())[:8]
    return {
        "episode_id": fake_id,
        "episode_url": f"https://www.spreaker.com/simulated-episode/{fake_id}",
        "simulated": True,
        "scheduled": scheduled,
        "backdated": backdated,
    }
