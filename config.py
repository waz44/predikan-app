"""
Central konfiguration. Läser in värden från .env (via python-dotenv).

De omkonfigurerbara inställningarna (OpenAI/Whisper/AI/Spreaker/e-post m.m.)
kan läsas om under körning med reload() - det använder inställningsguiden
(routers/setup.py) för att en sparad .env ska slå igenom direkt, utan
omstart. Det fungerar eftersom resten av appen läser config.X färskt vid
varje anrop (t.ex. spreaker_client.publish_episode läser
config.SPREAKER_API_TOKEN när den körs, inte vid import).

De strukturella värdena (kataloger, databasfil, loggfil) sätts en gång vid
import och ingår MEDVETET inte i reload() - att ändra dem live är riskabelt.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


def _resolve_version() -> str:
    """
    Appens version från EN källa (pyproject.toml). Läses i första hand ur
    den installerade paketmetadatan (pip install), annars direkt ur
    pyproject.toml när appen körs från källkoden utan att vara installerad.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        return _pkg_version("predikan-app")
    except PackageNotFoundError:
        try:
            import tomllib

            data = tomllib.loads((BASE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
            return data["project"]["version"]
        except Exception:
            return "0.0.0+unknown"


VERSION = _resolve_version()

# Ladda .env EXPLICIT från projektroten (bredvid denna fil), inte via en
# sökning relativt arbetskatalogen - appen kan startas från valfri katalog
# (t.ex. via konsollkommandot `predikan`), och inställningsguiden skriver
# .env just här. Utan explicit sökväg skulle reload() kunna missa filen.
_ENV_PATH = BASE_DIR / ".env"
load_dotenv(_ENV_PATH)

# --- Kataloger (strukturellt - sätts en gång vid import) ---
UPLOAD_DIR = BASE_DIR / os.getenv("UPLOAD_DIR", "uploads")
PROCESSED_DIR = BASE_DIR / os.getenv("PROCESSED_DIR", "processed")
BULK_IMPORT_DIR = BASE_DIR / os.getenv("BULK_IMPORT_DIR", "bulk_import")
UPLOAD_DIR.mkdir(exist_ok=True)
PROCESSED_DIR.mkdir(exist_ok=True)
BULK_IMPORT_DIR.mkdir(exist_ok=True)

# Lokalt arkiv av hela podden (mp3 + xml + txt per avsnitt, se
# modules/podcast_archive.py). Kan vara en absolut sökväg, t.ex. på en annan
# disk ("D:/Podcastarkiv") - en relativ sökväg räknas från projektroten.
# Skapas först när arkivet körs (inte här), så appen startar även om en
# extern disk råkar vara urkopplad.
ARCHIVE_DIR = BASE_DIR / os.getenv("ARCHIVE_DIR", "podcast_arkiv")

# Max antal predikningar (episoder) som sparas i uploads/ + processed/ samtidigt.
# När fler än så finns sparas bara de senaste - äldst bort-städas automatiskt
# efter varje lyckad bearbetning, så mapparna inte växer oändligt vid drift
# över lång tid. 0 (standard) = ingen begränsning, städa aldrig bort något.
MAX_STORED_EPISODES = int(os.getenv("MAX_STORED_EPISODES", "0") or "0")

# Största tillåtna uppladdning (MB) via /api/upload. Skydd mot att en
# jättefil (av misstag eller uppsåt) fyller disken. Predikoljud är sällan
# över några hundra MB; höj vid behov. 0 = ingen gräns.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500") or "0")
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# SQLite-databas för bearbetningskön och episodhistoriken/statistiken
# (se modules/db.py, modules/queue_store.py, modules/episode_store.py).
DATABASE_FILE = BASE_DIR / os.getenv("DATABASE_FILE", "predikan.db")

# --- Loggning ---
# Loggnivå för loggfilen (se modules/app_logging.py). Giltiga värden:
# DEBUG, INFO, WARNING, ERROR, CRITICAL. Standard: INFO.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = BASE_DIR / os.getenv("LOG_FILE", "app.log")

# Inställningsguiden (routers/setup.py) skriver .env och sköter OAuth utan
# autentisering - avsett för lokal enanvändardrift. Som skydd tillåts den
# BARA från loopback (127.0.0.1/::1) om inte detta uttryckligen sätts till
# true. Docker exponerar appen bakom en bryggnätverks-IP (inte loopback), så
# docker-compose.yml sätter den true - exponera då aldrig containern öppet
# utan en omvänd proxy med autentisering framför.
SETUP_ALLOW_REMOTE = os.getenv("SETUP_ALLOW_REMOTE", "false").lower() == "true"

# --- OpenAI (transkribering-fallback + molnbaserad AI-berikning) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USE_LOCAL_WHISPER = os.getenv("USE_LOCAL_WHISPER", "false").lower() == "true"
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "small")
# "auto" (default) = använd GPU (NVIDIA/CUDA) om PyTorch upptäcker en,
# annars CPU. Sätt till "cuda" eller "cpu" för att tvinga ett val.
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto").lower()

# --- AI-berikning (titel/beskrivning/taggar) ---
# "openai" = använd GPT via OpenAI API (kräver OPENAI_API_KEY)
# "ollama" = använd en lokal modell via Ollama (helt offline, ingen nyckel)
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

# --- Procentmätarens tidsuppskattning (påverkar bara UI:t, inte resultatet) ---
# Hur många sekunder bearbetning tar per sekund ljud, används för att rita
# en ungefärlig procentmätare för transkribering. Standard: 1.8 för lokal
# Whisper (CPU, "small"-modellen), 0.2 för OpenAI Whisper API. Om mätaren
# ofta fastnar länge på 95% på din dator, höj värdet här.
_whisper_factor_env = os.getenv("WHISPER_TIME_FACTOR", "").strip()
WHISPER_TIME_FACTOR = float(_whisper_factor_env) if _whisper_factor_env else None

# --- Spreaker ---
SPREAKER_API_TOKEN = os.getenv("SPREAKER_API_TOKEN", "")
SPREAKER_SHOW_ID = os.getenv("SPREAKER_SHOW_ID", "")
SPREAKER_SIMULATE = os.getenv("SPREAKER_SIMULATE", "true").lower() == "true"
SPREAKER_UPLOAD_URL = "https://api.spreaker.com/v2/shows/{show_id}/episodes"

# --- E-post ---
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")

# Vanliga ljudformat - stöds alla av ffmpeg/pydub oavsett vilket, eftersom
# hela pipelinen normaliserar om till mp3 direkt i klippningssteget
# (se modules/audio_processor.py) innan transkribering ens börjar, så
# ingen senare del av appen bryr sig om originalformatet.
# OBS: hålls i synk för hand med accept-attributet på filuppladdningen i
# static/index.html (statisk HTML, ingen mall/templating att generera det ur).
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma"}


def reload() -> None:
    """
    Läser om .env och uppdaterar de omkonfigurerbara inställningarna live.
    Används av inställningsguiden (routers/setup.py) efter att den skrivit
    nya värden till .env, så de slår igenom utan omstart. override=True
    krävs eftersom redan inlästa miljövariabler annars har företräde framför
    den uppdaterade .env-filen.

    Uppdaterar bara de icke-strukturella värdena - kataloger/DATABASE_FILE/
    LOG_FILE lämnas orörda (se moduldocstringen).
    """
    global MAX_STORED_EPISODES, LOG_LEVEL
    global OPENAI_API_KEY, USE_LOCAL_WHISPER, LOCAL_WHISPER_MODEL, WHISPER_DEVICE
    global AI_PROVIDER, OLLAMA_HOST, OLLAMA_MODEL, WHISPER_TIME_FACTOR
    global SPREAKER_API_TOKEN, SPREAKER_SHOW_ID, SPREAKER_SIMULATE
    global EMAIL_ENABLED, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL

    load_dotenv(_ENV_PATH, override=True)

    MAX_STORED_EPISODES = int(os.getenv("MAX_STORED_EPISODES", "0") or "0")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    USE_LOCAL_WHISPER = os.getenv("USE_LOCAL_WHISPER", "false").lower() == "true"
    LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "small")
    WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto").lower()

    AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()
    OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

    _factor = os.getenv("WHISPER_TIME_FACTOR", "").strip()
    WHISPER_TIME_FACTOR = float(_factor) if _factor else None

    SPREAKER_API_TOKEN = os.getenv("SPREAKER_API_TOKEN", "")
    SPREAKER_SHOW_ID = os.getenv("SPREAKER_SHOW_ID", "")
    SPREAKER_SIMULATE = os.getenv("SPREAKER_SIMULATE", "true").lower() == "true"

    EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")
