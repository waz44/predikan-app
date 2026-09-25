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
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

import config


class SpreakerUploadError(Exception):
    """
    Ett fel från Spreakers API, med ett meddelande som kan visas för användaren.

    Trots namnet används den för ALLA Spreaker-anrop (uppladdning, listning,
    redigering, nedladdning och inloggning) - namnet är kvar från när
    uppladdning var det enda anropet.
    """
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

    Args:
        publish_date: "YYYY-MM-DDTHH:MM" i datorns lokala tid.

    Raises:
        ValueError: Om texten inte är ett giltigt datum.
    """
    naive_local = datetime.fromisoformat(publish_date)
    aware_local = naive_local.astimezone()  # tolkar som datorns lokala tidszon
    utc_dt = aware_local.astimezone(UTC)
    return utc_dt.strftime("%Y-%m-%d %H:%M:%S"), utc_dt


def publish_episode(
    audio_path: Path,
    title: str,
    description: str,
    tags: list[str],
    publish_date: str = "",
    progress_callback: Callable[[int], None] | None = None,
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

    Args:
        audio_path: Den klippta mp3-filen som ska laddas upp.
        title: Avsnittets titel.
        description: Beskrivningen (ren text - HTML tas bort av Spreaker).
        tags: Taggar, skickas kommaseparerade.

    Raises:
        SpreakerUploadError: Vid ogiltigt datum, misslyckad uppladdning
            eller misslyckad bakåtdatering.
    """
    simulate = (
        config.SPREAKER_SIMULATE
        or not config.SPREAKER_API_TOKEN
        or not config.SPREAKER_SHOW_ID
    )

    auto_published_at = None  # sätts bara om vi faktiskt ska schemalägga
    backdate_utc: str | None = None
    scheduled = False
    backdated = False

    if publish_date:
        try:
            formatted_utc, utc_dt = _format_publish_date(publish_date)
        except ValueError as exc:
            raise SpreakerUploadError(f"Ogiltigt publiceringsdatum: {exc}") from exc

        now_utc = datetime.now(UTC)
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

    Returns:
        En lista med ett fullständigt avsnitt (dict) per avsnitt på kontot.

    Raises:
        SpreakerUploadError: Om Spreaker svarar med ett fel.
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
    """
    Hämtar FULLSTÄNDIGA fält för ETT avsnitt, inklusive description/plays_count (se list_episodes).

    Args:
        episode_id: Spreakers id för avsnittet.

    Returns:
        Avsnittet som dict, med de fält Spreaker returnerar (title,
        description, duration i ms, published_at, site_url, plays_count ...).

    Raises:
        SpreakerUploadError: Om avsnittet inte finns eller token saknar behörighet.
    """
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
    """
    Redigerar titel/beskrivning för ett REDAN publicerat avsnitt på Spreaker.

    Ändringen syns direkt på Spreaker och i podcastappar nästa gång de
    hämtar flödet. Ljudfilen och publiceringsdatumet påverkas inte.

    Args:
        episode_id: Spreakers id för avsnittet.
        title: Den nya titeln.
        description: Den nya beskrivningen (ren text).

    Raises:
        SpreakerUploadError: Om Spreaker avvisar ändringen.
    """
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


def download_episode_audio(episode_id: int, dest_path: Path) -> None:
    """
    Laddar ner ljudfilen för ETT REDAN publicerat avsnitt (för
    "Generera om"-funktionen i Hantera Spreaker-fliken, som behöver
    transkribera avsnittet på nytt - Spreaker har inget eget transkript
    att återanvända, se services/pipeline.py:_run_regenerate_job).
    Strömmas till disk i bitar eftersom predikoljud kan vara stora filer.

    Args:
        episode_id: Spreakers id för avsnittet.
        dest_path: Var ljudfilen ska sparas (mappen skapas vid behov).

    Raises:
        SpreakerUploadError: Om Spreaker inte levererar filen.
    """
    response = requests.get(
        f"https://api.spreaker.com/v2/episodes/{episode_id}/download.mp3",
        headers={"Authorization": f"Bearer {config.SPREAKER_API_TOKEN}"},
        timeout=300,
        stream=True,
    )
    if response.status_code != 200:
        raise SpreakerUploadError(
            f"Kunde inte hämta ljudfilen för avsnitt {episode_id} från Spreaker ({response.status_code})."
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)


def _simulate_publish(
    title: str,
    scheduled: bool,
    backdated: bool,
    progress_callback: Callable[[int], None] | None = None,
) -> dict:
    """
    Simulerar en Spreaker-publicering (ingen internetanslutning krävs).

    Används när SPREAKER_SIMULATE=true eller när token/show-id saknas, så att
    hela flödet kan provas utan ett riktigt konto. Procentmätaren stegar
    igenom några värden så att uppladdningen ser ut att pågå en kort stund.

    Args:
        title: Avsnittets titel (används inte, men håller anropet likt det riktiga).
        scheduled: Om avsnittet skulle ha schemalagts.
        backdated: Om avsnittet skulle ha bakåtdaterats.
        progress_callback: Anropas med procent, som vid en riktig uppladdning.

    Returns:
        Samma sorts svar som publish_episode, med simulated=True och en påhittad länk.
    """
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


# ---------------------------------------------------------------------------
# OAuth-/kontohjälpare för inställningsguiden (routers/setup.py)
#
# Dessa tar token/credentials som ARGUMENT i stället för att läsa config,
# eftersom de körs INNAN något är sparat i .env - det är ju precis det
# guiden hjälper användaren att fylla i. Övriga funktioner ovan använder
# config.SPREAKER_API_TOKEN som vanligt, för det redan konfigurerade läget.
# ---------------------------------------------------------------------------
SPREAKER_AUTHORIZE_URL = "https://www.spreaker.com/oauth2/authorize"
SPREAKER_TOKEN_URL = "https://api.spreaker.com/oauth2/token"


def build_authorize_url(client_id: str, redirect_uri: str, state: str = "predikan") -> str:
    """
    Bygger URL:en användaren öppnar för att godkänna appen och få en auktoriseringskod.

    Args:
        client_id: Appens Client ID från Spreakers utvecklarsida.
        redirect_uri: Dit Spreaker skickar användaren efter godkännandet.
            "http://localhost" räcker - sidan behöver inte fungera, koden
            läses ur adressfältet.
        state: Valfritt värde som skickas tillbaka oförändrat.

    Returns:
        Hela adressen till Spreakers godkännandesida.
    """
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "response_type": "code",
        "state": state,
        "scope": "basic",
        "redirect_uri": redirect_uri,
    }
    return f"{SPREAKER_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_oauth_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> str:
    """
    Byter en auktoriseringskod mot en access-token (server-side, så
    användaren slipper köra curl för hand - se README-avsnittet om Spreaker).
    Returnerar själva access-token-strängen.

    Args:
        client_id: Appens Client ID.
        client_secret: Appens Client Secret.
        redirect_uri: Samma adress som användes i build_authorize_url.
        code: Koden från adressfältet efter godkännandet.

    Raises:
        SpreakerUploadError: Om koden är ogiltig, redan använd eller för gammal.
    """
    response = requests.post(
        SPREAKER_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=60,
    )
    if response.status_code not in (200, 201):
        raise SpreakerUploadError(
            f"Kunde inte byta koden mot en token ({response.status_code}): {response.text}"
        )
    token = response.json().get("response", {}).get("access_token")
    if not token:
        raise SpreakerUploadError(f"Spreaker-svaret saknade access_token: {response.text}")
    return token


def get_me(token: str) -> dict:
    """
    Hämtar den inloggade användaren för en given token - används för att verifiera att token fungerar.

    Args:
        token: En Spreaker-token att kontrollera.

    Returns:
        Användaren som dict (user_id, fullname m.m.).

    Raises:
        SpreakerUploadError: Om token är ogiltig.
    """
    response = requests.get(
        "https://api.spreaker.com/v2/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if response.status_code != 200:
        raise SpreakerUploadError(
            f"Token verifierades inte mot Spreaker ({response.status_code}): {response.text}"
        )
    return response.json().get("response", {}).get("user", {})


def list_my_shows(token: str) -> list[dict]:
    """
    Listar den inloggade användarens shows (podcasts) för en given token, så
    inställningsguiden kan låta användaren VÄLJA sitt show i stället för att
    leta upp det numeriska show-id:t för hand. Returnerar en förenklad lista
    med bara show_id + title.

    Args:
        token: En giltig Spreaker-token.

    Returns:
        En lista med {"show_id": ..., "title": ...} per show.

    Raises:
        SpreakerUploadError: Om token är ogiltig eller listan inte kan hämtas.
    """
    user = get_me(token)
    user_id = user.get("user_id")
    if not user_id:
        raise SpreakerUploadError("Kunde inte läsa användar-id från Spreaker.")

    url = f"https://api.spreaker.com/v2/users/{user_id}/shows"
    headers = {"Authorization": f"Bearer {token}"}
    shows: list[dict] = []
    while url:
        response = requests.get(url, headers=headers, timeout=60)
        if response.status_code != 200:
            raise SpreakerUploadError(
                f"Kunde inte hämta dina shows från Spreaker ({response.status_code}): {response.text}"
            )
        payload = response.json().get("response", {})
        for item in payload.get("items", []):
            shows.append({"show_id": item.get("show_id"), "title": item.get("title", "")})
        url = payload.get("next_url")
    return shows
