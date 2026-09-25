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
# sqlite3: SQLite ingår i Python - ingen databasserver behöver installeras.
import sqlite3

# threading.local: lagring som är separat för varje tråd (se nedan).
import threading

# contextmanager gör get_connection() användbar med "with".
from contextlib import contextmanager

# config.DATABASE_FILE: sökvägen till databasfilen (standard predikan.db).
import config

# Trådlokal lagring: varje tråd ser sin EGEN "database_file" här. Bara
# kö-arbetartråden sätter något (via bind_thread_to_database_file) - för
# alla andra trådar saknas värdet och config.DATABASE_FILE används.
_thread_local = threading.local()


def bind_thread_to_database_file(database_file) -> None:
    """Fäster den anropande tråden vid ANGIVEN databasfil för resten av dess livstid (se moduldocstringen).

    Tar filen som argument istället för att läsa config.DATABASE_FILE här -
    anroparen (app.py:_on_startup) fångar värdet i huvudtråden innan
    arbetartråden startas, se moduldocstringen för varför.
    """
    _thread_local.database_file = database_file


def _connect() -> sqlite3.Connection:
    """
    Öppnar en ny SQLite-anslutning med appens standardinställningar.

    Returns:
        En öppen anslutning. Anroparen ansvarar för att stänga den - använd
        därför alltid get_connection() i stället för att anropa denna direkt.
    """
    # Trådens egen fil om den har bundits till en, annars den konfigurerade.
    database_file = getattr(_thread_local, "database_file", None) or config.DATABASE_FILE
    # timeout=30: om en annan anslutning just skriver väntar vi upp till 30 s
    # på att låset släpps, i stället för att direkt få "database is locked".
    conn = sqlite3.connect(str(database_file), timeout=30)
    # Rader som sqlite3.Row kan läsas med kolumnnamn: row["status"].
    conn.row_factory = sqlite3.Row
    # WAL (write-ahead logging): läsare och en skrivare kan arbeta samtidigt
    # utan att låsa ut varandra - viktigt när kö-arbetartråden skriver medan
    # webbsidan läser köns status. Inställningen sparas i själva filen.
    conn.execute("PRAGMA journal_mode=WAL")
    # Slår på kontroll av främmande nycklar (av som standard i SQLite).
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_connection():
    """
    Kortlivad databasanslutning som context manager: "with get_connection() as conn:".

    Allt som görs inne i with-blocket blir en transaktion: lyckas blocket
    sparas ändringarna (commit), kastas ett fel ångras de (rollback) och
    felet skickas vidare. Anslutningen stängs alltid efteråt.

    Yields:
        En öppen sqlite3.Connection.
    """
    conn = _connect()
    try:
        # Här körs koden inne i anroparens with-block.
        yield conn
        # Inget fel: spara alla ändringar på en gång.
        conn.commit()
    except Exception:
        # Något gick fel: ångra ALLA ändringar i blocket, så databasen aldrig
        # blir halvuppdaterad - och skicka felet vidare till anroparen.
        conn.rollback()
        raise
    finally:
        # Stäng alltid - en anslutning per anrop är enklare och säkrare
        # mellan trådar än en delad, långlivad anslutning.
        conn.close()


# Databasens tabeller. Kort om var och en:
#
# queue_items - bearbetningskön. En rad per köat jobb, i ordningen
#   "position". status går queued -> running -> done/error/cancelled.
#   fields (formulärets värden) och result (resultatet) är JSON-text.
#   base_name sätts när jobbet startar och används för att hitta och
#   städa jobbets filer.
#
# episodes - historik över LYCKADE bearbetningar. Ger statistiken
#   (predikans längd mot bearbetningstid) och avgör vilka episoder som är
#   äldst när MAX_STORED_EPISODES begränsar lagringen. Flaggorna
#   (simulated, scheduled ...) är 0/1 eftersom SQLite saknar boolesk typ.
#
# spreaker_episodes - lokal kopia av avsnitten på Spreaker-kontot (se
#   kommentaren i SQL:en nedan).
#
# spreaker_transcripts - sparade transkript för "Generera om".
#
# app_state - en enda rad med körlägesinställningar (om kön är pausad).
#
# Indexen på status, position och created_at gör de vanligaste frågorna
# ("nästa väntande", "köordning", "äldst först") snabba även med lång historik.
#
# Alla tidpunkter lagras som ISO-text ("2026-09-21T10:30:00"), som sorteras
# rätt även alfabetiskt och är läsbar direkt i databasen.
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

-- Lokal CACHE av vad som faktiskt ligger på det riktiga Spreaker-kontot
-- (modules/spreaker_episode_store.py) - separat från episodes ovan, som
-- bara loggar appens EGNA lyckade publiceringar. Fylls om helt vid varje
-- "Hämta från Spreaker" (se replace_all), så borttagna avsnitt på kontot
-- försvinner ur cachen också.
CREATE TABLE IF NOT EXISTS spreaker_episodes (
    episode_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    speaker TEXT,
    published_at TEXT,
    duration_seconds REAL,
    plays_count INTEGER,
    site_url TEXT,
    fetched_at TEXT NOT NULL
);

-- Cachade transkript för "Generera om"-funktionen i Hantera Spreaker-
-- fliken (modules/spreaker_episode_store.py) - MEDVETET en EGEN tabell,
-- inte en kolumn på spreaker_episodes ovan, eftersom den tabellen töms
-- och fylls om helt vid varje "Hämta från Spreaker" (replace_all). Ett
-- redan nedladdat/transkriberat avsnitt ska inte behöva transkriberas om
-- bara för att avsnittslistan uppdaterats.
CREATE TABLE IF NOT EXISTS spreaker_transcripts (
    episode_id INTEGER PRIMARY KEY,
    transcript TEXT NOT NULL,
    transcribed_at TEXT NOT NULL
);

-- En enda rad (id=1) med appens beständiga körlägesinställningar - just nu
-- bara om kön är pausad. Litet nog för en enkel key-value-tabell, men en
-- vanlig tabell med en rad är enklare att fråga/uppdatera med vanlig SQL.
CREATE TABLE IF NOT EXISTS app_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0
);
"""


def init_db() -> None:
    """
    Skapar tabeller/index om de inte redan finns. Körs en gång vid appstart.

    Säker att köra flera gånger: "IF NOT EXISTS" och "INSERT OR IGNORE" gör
    att en befintlig databas lämnas orörd, med all sin data.
    """
    # Mappen kan saknas, t.ex. data/ i Docker (DATABASE_FILE=data/predikan.db).
    config.DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        # executescript kör alla CREATE-satserna i SCHEMA i en följd.
        conn.executescript(SCHEMA)
        # Se till att app_state-raden finns; en befintlig rad (och därmed
        # ett sparat pausläge) lämnas orörd tack vare OR IGNORE.
        conn.execute("INSERT OR IGNORE INTO app_state (id, paused) VALUES (1, 0)")
