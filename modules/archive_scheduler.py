"""
Modul: archive_scheduler
Schemalagd arkivering av podden (se modules/podcast_archive.py).

Inställningarna (ARCHIVE_SCHEDULE, ARCHIVE_SCHEDULE_DAY och
ARCHIVE_SCHEDULE_TIME, se config.py) sätts under ⚙️ Inställningar →
🗄️ Podd-arkiv. En bakgrundstråd (run_loop, startas av app.py) tittar två
gånger i minuten om det är dags och startar då en vanlig arkivkörning -
precis som knappen "Arkivera nu".

Appen (eller datorn) måste vara igång vid den valda tiden. Missas en tid
körs arkiveringen vid nästa tillfälle - inte i efterhand.
"""
import threading
from datetime import datetime, timedelta

import config
from modules import app_logging, podcast_archive

# Hur ofta tråden tittar om det är dags (sekunder).
_CHECK_INTERVAL = 30
# Används om ARCHIVE_SCHEDULE_TIME inte går att tolka.
_DEFAULT_TIME = (3, 0)


def _parse_time(text: str) -> tuple[int, int]:
    """
    "HH:MM" som (timme, minut), eller 03:00 om texten inte går att tolka.

    Args:
        text: T.ex. "03:00" eller "22:30".

    Returns:
        (timme, minut).
    """
    try:
        hour, minute = (int(part) for part in text.strip().split(":"))
    except (ValueError, AttributeError):
        return _DEFAULT_TIME
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return _DEFAULT_TIME


def next_run(now: datetime, schedule: str, day: int, time_text: str) -> datetime | None:
    """
    Nästa gång arkiveringen ska köras, räknat från now (alltid efter now).

    Args:
        now: Nuvarande tid.
        schedule: "off", "daily" eller "weekly".
        day: Veckodag för "weekly", 0 = måndag ... 6 = söndag.
        time_text: Klockslaget, "HH:MM".

    Returns:
        Tidpunkten, eller None när schemat är avstängt.
    """
    if schedule not in ("daily", "weekly"):
        return None
    hour, minute = _parse_time(time_text)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if schedule == "daily":
        return candidate if candidate > now else candidate + timedelta(days=1)
    candidate += timedelta(days=(day - now.weekday()) % 7)
    return candidate if candidate > now else candidate + timedelta(days=7)


def next_scheduled_run() -> datetime | None:
    """Nästa schemalagda körning enligt de inställningar som gäller just nu."""
    return next_run(datetime.now(), config.ARCHIVE_SCHEDULE, config.ARCHIVE_SCHEDULE_DAY, config.ARCHIVE_SCHEDULE_TIME)


def run_loop(stop_event: threading.Event) -> None:
    """
    Bakgrundstrådens slinga: startar en arkivering när det är dags.

    Nästa tid räknas om när inställningarna ändras (de kan sparas medan
    appen kör). Körs tills stop_event sätts vid nedstängning.

    Args:
        stop_event: Sätts av app.py när appen stängs.
    """
    settings = None
    due: datetime | None = None
    while True:
        current = (config.ARCHIVE_SCHEDULE, config.ARCHIVE_SCHEDULE_DAY, config.ARCHIVE_SCHEDULE_TIME)
        if current != settings:
            settings = current
            due = next_run(datetime.now(), *current)
        if due and datetime.now() >= due:
            if not podcast_archive.is_available():
                app_logging.logger.warning("Schemalagd arkivering hoppades över - SPREAKER_SHOW_ID saknas.")
            elif podcast_archive.start():
                app_logging.logger.info("Schemalagd arkivering startad.")
            else:
                app_logging.logger.info("Schemalagd arkivering hoppades över - en arkivering pågår redan.")
            due = next_run(datetime.now(), *current)
        if stop_event.wait(_CHECK_INTERVAL):
            return
