"""
Router: setup
Inställningsguiden (fliken "⚙️ Inställningar" i frontend). Låter användaren
fylla i och spara .env-värden via GUI:t i stället för att handredigera filen,
och sköter den annars krångliga Spreaker-OAuth-proceduren åt användaren:
bygger auktoriseringslänken, byter koden mot en token server-side, verifierar
den och listar användarens shows så rätt show-id kan väljas i en lista.

Sparade värden skrivs till .env (modules/env_file.py) och läses in live med
config.reload(), så de flesta inställningar (Spreaker/OpenAI/AI) slår igenom
direkt utan omstart - resten av appen läser config.X färskt vid varje anrop.

SÄKERHET: endpointsen skriver till .env och är därför avsiktligt begränsade -
bara nycklar i ALLOWED_KEYS får sparas (ingen godtycklig injektion), och
appen binder som standard till 127.0.0.1 (lokal enanvändarapp). Hemligheter
maskeras i GET /config så de aldrig skickas tillbaka till webbläsaren i klartext.
"""
from urllib.parse import parse_qs, urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import config
from modules import env_file, spreaker_client
from modules.spreaker_client import SpreakerUploadError

# Loopback-adresser som alltid får nå setup-endpointsen. "testclient" är den
# host Starlettes TestClient använder, så testerna slipper sätta upp nätverk.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _require_local_access(request: Request) -> None:
    """
    Släpper bara igenom anrop från loopback, om inte config.SETUP_ALLOW_REMOTE
    är satt. Setup-endpointsen skriver .env och sköter OAuth utan autentisering,
    så de ska inte gå att nå från nätverket som standard (se config.py).
    """
    if config.SETUP_ALLOW_REMOTE:
        return
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="Inställningsguiden är bara tillgänglig lokalt (från samma dator). "
            "Sätt SETUP_ALLOW_REMOTE=true i .env för att tillåta fjärråtkomst "
            "(gör det bara bakom en autentiserad omvänd proxy).",
        )


router = APIRouter(prefix="/api/setup", tags=["setup"], dependencies=[Depends(_require_local_access)])

# Nycklar som guiden får skriva till .env. Medvetet INTE med: kataloger,
# DATABASE_FILE och LOG_FILE - de är strukturella och att ändra dem live är
# riskabelt (se config.py, där de sätts en gång vid import). Undantag:
# ARCHIVE_DIR, som bara läses när en arkivkörning startar.
ALLOWED_KEYS = {
    "OPENAI_API_KEY",
    "USE_LOCAL_WHISPER",
    "LOCAL_WHISPER_MODEL",
    "WHISPER_DEVICE",
    "WHISPER_TIME_FACTOR",
    "AI_PROVIDER",
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
    "MAX_STORED_EPISODES",
    "SPREAKER_API_TOKEN",
    "SPREAKER_SHOW_ID",
    "SPREAKER_SIMULATE",
    "EMAIL_ENABLED",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "NOTIFY_EMAIL",
    "LOG_LEVEL",
    "ARCHIVE_DIR",
}

# Nycklar vars värde aldrig skickas tillbaka i klartext till frontend.
_SECRET_KEYS = {"OPENAI_API_KEY", "SPREAKER_API_TOKEN", "SMTP_PASSWORD"}


class AuthorizeUrlRequest(BaseModel):
    client_id: str
    redirect_uri: str = "http://localhost"


class ExchangeRequest(BaseModel):
    client_id: str
    client_secret: str
    redirect_uri: str = "http://localhost"
    # Antingen själva koden, eller hela redirect-URL:en (t.ex.
    # "http://localhost/?state=...&code=ABC") - då plockas code ut åt användaren.
    code: str


class TokenRequest(BaseModel):
    token: str


class OpenAIKeyRequest(BaseModel):
    api_key: str


class SaveRequest(BaseModel):
    values: dict[str, str]


def _mask(value: str) -> str:
    """Maskerar en hemlighet men visar de sista tecknen, så användaren ser att NÅGOT är satt."""
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return "••••" + value[-4:]


@router.get("/config")
async def get_config():
    """
    Nuvarande inställningar för guiden. Hemligheter maskeras (bara om de är
    satta + de sista tecknen), övriga värden skickas som de är.
    """
    return {
        "openai_api_key_set": bool(config.OPENAI_API_KEY),
        "openai_api_key_masked": _mask(config.OPENAI_API_KEY),
        "use_local_whisper": config.USE_LOCAL_WHISPER,
        "local_whisper_model": config.LOCAL_WHISPER_MODEL,
        "whisper_device": config.WHISPER_DEVICE,
        "whisper_time_factor": config.WHISPER_TIME_FACTOR,
        "ai_provider": config.AI_PROVIDER,
        "ollama_host": config.OLLAMA_HOST,
        "ollama_model": config.OLLAMA_MODEL,
        "max_stored_episodes": config.MAX_STORED_EPISODES,
        "spreaker_api_token_set": bool(config.SPREAKER_API_TOKEN),
        "spreaker_api_token_masked": _mask(config.SPREAKER_API_TOKEN),
        "spreaker_show_id": config.SPREAKER_SHOW_ID,
        "spreaker_simulate": config.SPREAKER_SIMULATE,
        "email_enabled": config.EMAIL_ENABLED,
        "smtp_host": config.SMTP_HOST,
        "smtp_port": config.SMTP_PORT,
        "smtp_user": config.SMTP_USER,
        "smtp_password_set": bool(config.SMTP_PASSWORD),
        "notify_email": config.NOTIFY_EMAIL,
        "log_level": config.LOG_LEVEL,
        "archive_dir": config.ARCHIVE_DIR_SETTING,
        "archive_dir_resolved": str(config.ARCHIVE_DIR),
    }


@router.post("/spreaker/authorize-url")
async def spreaker_authorize_url(req: AuthorizeUrlRequest):
    """Bygger auktoriseringslänken användaren öppnar för att få en kod (steg 2 i guiden)."""
    if not req.client_id.strip():
        raise HTTPException(status_code=400, detail="Client ID är obligatoriskt.")
    url = spreaker_client.build_authorize_url(req.client_id.strip(), req.redirect_uri.strip())
    return {"url": url}


def _extract_code(code_or_url: str) -> str:
    """Tar antingen en rå kod eller hela redirect-URL:en och returnerar koden."""
    value = code_or_url.strip()
    if value.startswith("http://") or value.startswith("https://"):
        query = parse_qs(urlparse(value).query)
        found = query.get("code", [""])[0]
        if not found:
            raise HTTPException(status_code=400, detail="Hittade ingen 'code' i den inklistrade URL:en.")
        return found
    return value


@router.post("/spreaker/exchange")
async def spreaker_exchange(req: ExchangeRequest):
    """
    Byter auktoriseringskoden mot en token (steg 3), verifierar den och
    listar användarens shows (steg 4) - allt i ett anrop. Sparar INGET; det
    gör användaren själv via /save efter att ha valt show.
    """
    if not req.client_id.strip() or not req.client_secret.strip() or not req.code.strip():
        raise HTTPException(status_code=400, detail="Client ID, Client Secret och kod är obligatoriska.")
    code = _extract_code(req.code)
    try:
        token = spreaker_client.exchange_oauth_code(
            req.client_id.strip(), req.client_secret.strip(), req.redirect_uri.strip(), code
        )
        user = spreaker_client.get_me(token)
        shows = spreaker_client.list_my_shows(token)
    except SpreakerUploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"token": token, "user": user, "shows": shows}


@router.post("/spreaker/verify")
async def spreaker_verify(req: TokenRequest):
    """
    Verifierar en redan befintlig token (för den som klistrar in en token
    direkt i stället för att köra OAuth-flödet) och listar dess shows.
    """
    if not req.token.strip():
        raise HTTPException(status_code=400, detail="Token är obligatoriskt.")
    try:
        user = spreaker_client.get_me(req.token.strip())
        shows = spreaker_client.list_my_shows(req.token.strip())
    except SpreakerUploadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"user": user, "shows": shows}


@router.post("/openai/verify")
async def openai_verify(req: OpenAIKeyRequest):
    """Verifierar en OpenAI-nyckel genom ett lätt anrop mot /v1/models."""
    key = req.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="API-nyckel är obligatoriskt.")
    try:
        response = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Kunde inte nå OpenAI: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"OpenAI avvisade nyckeln ({response.status_code}).",
        )
    return {"valid": True}


@router.post("/save")
async def save_settings(req: SaveRequest):
    """
    Skriver de angivna värdena till .env och läser om konfigurationen live.
    Bara nycklar i ALLOWED_KEYS accepteras. En hemlighet med tomt värde
    hoppas över (så en oförändrad, maskerad hemlighet inte råkar nollställas).
    """
    unknown = set(req.values) - ALLOWED_KEYS
    if unknown:
        raise HTTPException(status_code=400, detail=f"Otillåtna nycklar: {', '.join(sorted(unknown))}")

    updates: dict[str, str] = {}
    for key, value in req.values.items():
        if key in _SECRET_KEYS and value.strip() == "":
            continue  # lämna en redan sparad hemlighet orörd
        updates[key] = value.strip()

    if not updates:
        return await get_config()

    env_file.set_values(updates)
    config.reload()
    return await get_config()
