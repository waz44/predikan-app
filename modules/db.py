"""
Modul: db
Central SQLite-anslutning och schema för appens beständiga tillstånd:
bearbetningskön (modules/queue_store.py) och episodhistoriken/statistiken
(modules/episode_store.py).

SQLite valdes eftersom appen är en lokal enanvändarapp - ingen separat
databasserver behövs, och sqlite3 ingår i Python. WAL-läge ("Write-Ahead
Logging") aktiveras för att kö-arbetartråden (som kan skriva) och
request-hanterare (som läser) ska kunna arbeta samtidigt utan att låsa ut
varandra i onödan.

En kort-levad anslutning öppnas per anrop (get_connection() som context
manager) istället för att dela en enda global anslutning mellan trådar -
sqlite3-anslutningar är inte trådsäkra att dela rakt av, och SQLite-filen
själv hanterar samtidig åtkomst via sitt eget fillås.

_connect() läser normalt config.DATABASE_FILE direkt (så att t.ex.
request-hanterare alltid ser den aktuella, ev. omkonfigurerade, sökvägen).
Den enda kö-arbetartråden (services/pipeline.py:queue_worker_loop) är
undantaget: den fäster sig vid den sökväg som gällde när TRÅDEN STARTADES
(se bind_thread_to_database_file) för resten av sin livstid. Utan det
skulle en arbetartråd som fortfarande höll på att avsluta ett jobb när
config.DATABASE_FILE pekas om (t.ex. mellan pytest-tester, som var och en
monkeypatchar den till en egen temp-databas) kunna hinna göra ytterligare
ett databasanrop mot FEL databas.

Sökvägen skickas in som ARGUMENT till bind_thread_to_database_file (fångad
av app.py:_on_startup i huvudtråden, precis innan arbetartråden startas)
istället för att arbetartråden själv läser config.DATABASE_FILE när den
kör igång. threading.Thread.start() returnerar så fort tråden är
SCHEMALAGD, inte när den faktiskt fått köra sin första rad kod - under
belastning kan de ligga sekunder isär. Om arbetartråden läste
config.DATABASE_FILE själv skulle den kunna hinna se ett HELT ANNAT
tests värde (t.ex. om detta tests _on_shutdown redan gett upp på att
vänta in föregående tråd, se app.py, och nästa test redan monkeypatchat
om vägen innan denna tråd ens hunnit köra sin första rad).
"""
import sqlite3
import threading
from contextlib import contextmanager

import config

_thread_local = threading.local()


def bind_thread_to_database_file(database_file) -> None:
    """Fäster den anropande tråden vid ANGIVEN databasfil för resten av dess livstid (se moduldocstringen).

    Tar filen som argument istället för att läsa config.DATABASE_FILE här -
    anroparen (app.py:_on_startup) fångar värdet i huvudtråden innan
    arbetartråden startas, se moduldocstringen för varför.
    """
    _thread_local.database_file = database_file


def _connect() -> sqlite3.Connection:
    database_file = getattr(_thread_local, "database_file", None) or config.DATABASE_FILE
    conn = sqlite3.connect(str(database_file), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_connection():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_items (
    queue_id TEXT PRIMARY KEY,
    job_id TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    speaker TEXT NOT NULL,
    fields TEXT NOT NULL,
    original_path TEXT NOT NULL,
    keep_original INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    base_name TEXT,
    overall_percent INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    result TEXT,
    position INTEGER NOT NULL,
    queued_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_items_status ON queue_items(status);
CREATE INDEX IF NOT EXISTS idx_queue_items_position ON queue_items(position);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_name TEXT UNIQUE NOT NULL,
    speaker TEXT NOT NULL,
    title TEXT,
    description TEXT,
    tags TEXT,
    publish_date TEXT,
    category TEXT,
    kind TEXT NOT NULL,
    episode_url TEXT,
    simulated INTEGER NOT NULL DEFAULT 0,
    scheduled INTEGER NOT NULL DEFAULT 0,
    backdated INTEGER NOT NULL DEFAULT 0,
    email_sent INTEGER NOT NULL DEFAULT 0,
    audio_path TEXT,
    transcript_path TEXT,
    enrichment_path TEXT,
    sermon_seconds REAL NOT NULL,
    processing_seconds REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_created_at ON episodes(created_at);

-- En enda rad (id=1) med appens beständiga körlägesinställningar - just nu
-- bara om kön är pausad. Litet nog för en enkel key-value-tabell, men en
-- vanlig tabell med en rad är enklare att fråga/uppdatera med vanlig SQL.
CREATE TABLE IF NOT EXISTS app_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0
);
"""


def init_db() -> None:
    """Skapar tabeller/index om de inte redan finns. Körs en gång vid appstart."""
    config.DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO app_state (id, paused) VALUES (1, 0)")
