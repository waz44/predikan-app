"""
Central konfiguration. Läser in värden från .env (via python-dotenv).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# --- Kataloger ---
UPLOAD_DIR = BASE_DIR / os.getenv("UPLOAD_DIR", "uploads")
PROCESSED_DIR = BASE_DIR / os.getenv("PROCESSED_DIR", "processed")
UPLOAD_DIR.mkdir(exist_ok=True)
PROCESSED_DIR.mkdir(exist_ok=True)

# --- OpenAI (transkribering-fallback + molnbaserad AI-berikning) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USE_LOCAL_WHISPER = os.getenv("USE_LOCAL_WHISPER", "false").lower() == "true"
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "small")

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
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")

ALLOWED_EXTENSIONS = {".mp3", ".wav"}
