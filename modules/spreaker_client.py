"""
Modul: spreaker_client
Laddar upp ett färdigt avsnitt till Spreaker via deras publika API:
https://developers.spreaker.com/api-v2/#!/episodes/post_shows_show_id_episodes

Om SPREAKER_SIMULATE=true (eller om token/show-id saknas) simuleras
uppladdningen istället, så att hela flödet kan testas utan riktiga
API-nycklar.

Stöder även schemalagd publicering via `publish_date` (annars publiceras
avsnittet direkt), samt en `progress_callback` som anropas löpande med
verklig uppladdningsprocent (0-100) medan filen skickas till Spreaker.
"""
from pathlib import Path
from typing import Callable, Optional
from datetime import datetime
import time
import uuid
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
import config


class SpreakerUploadError(Exception):
    pass


def _format_publish_date(publish_date: str) -> str:
    """
    Konverterar värdet från ett <input type="datetime-local"> ("YYYY-MM-DDTHH:MM")
    till det format Spreakers API förväntar sig ("YYYY-MM-DD HH:MM:SS").
    """
    dt = datetime.fromisoformat(publish_date)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


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
        publish_date: Valfritt, "YYYY-MM-DDTHH:MM" för schemalagd publicering.
                      Tomt = publiceras direkt.
        progress_callback: Valfri funktion som anropas med uppladdningsprocent (0-100).

    Returns:
        dict med minst nycklarna "episode_url" och "episode_id".
    """
    simulate = (
        config.SPREAKER_SIMULATE
        or not config.SPREAKER_API_TOKEN
        or not config.SPREAKER_SHOW_ID
    )

    auto_published_at = "now"
    scheduled = False
    if publish_date:
        try:
            auto_published_at = _format_publish_date(publish_date)
            scheduled = True
        except ValueError as exc:
            raise SpreakerUploadError(f"Ogiltigt publiceringsdatum: {exc}")

    if simulate:
        return _simulate_publish(title, scheduled, progress_callback)

    url = config.SPREAKER_UPLOAD_URL.format(show_id=config.SPREAKER_SHOW_ID)
    headers = {"Authorization": f"Bearer {config.SPREAKER_API_TOKEN}"}

    with open(audio_path, "rb") as audio_file:
        encoder = MultipartEncoder(
            fields={
                "title": title,
                "description": description,
                "tags": ",".join(tags) if tags else "",
                "auto_published_at": auto_published_at,
                "media_file": (audio_path.name, audio_file, "audio/mpeg"),
            }
        )

        def _on_progress(monitor: MultipartEncoderMonitor) -> None:
            if progress_callback and monitor.len:
                percent = int(monitor.bytes_read / monitor.len * 100)
                # Håll den på max 99% tills vi faktiskt fått ett svar från Spreaker
                progress_callback(min(percent, 99))

        monitor = MultipartEncoderMonitor(encoder, _on_progress)
        headers["Content-Type"] = monitor.content_type

        response = requests.post(url, headers=headers, data=monitor, timeout=600)

    if response.status_code not in (200, 201):
        raise SpreakerUploadError(
            f"Spreaker-uppladdning misslyckades ({response.status_code}): {response.text}"
        )

    if progress_callback:
        progress_callback(100)

    payload = response.json().get("response", {}).get("episode", {})
    episode_id = payload.get("episode_id")
    episode_url = payload.get("site_url") or f"https://www.spreaker.com/episode/{episode_id}"

    return {
        "episode_id": episode_id,
        "episode_url": episode_url,
        "simulated": False,
        "scheduled": scheduled,
    }


def _simulate_publish(
    title: str,
    scheduled: bool,
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
    }
