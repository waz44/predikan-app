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
# parse_qs/urlparse: plocka ut "code=" ur en inklistrad adress.
from urllib.parse import parse_qs, urlparse

# requests: provanropet mot OpenAI när en nyckel verifieras.
import requests

# Depends: kör _require_local_access före varje endpoint. Request: ger
# avsändarens IP-adress. BaseModel: beskriver vad varje anrop tar emot.
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import config

# ai_enrichment: standardprompterna. env_file: skriver .env.
# spreaker_client: OAuth-anropen mot Spreaker.
from modules import ai_enrichment, env_file, spreaker_client
from modules.spreaker_client import SpreakerUploadError

# Loopback-adresser som alltid får nå setup-endpointsen. "testclient" är den
# host Starlettes TestClient använder, så testerna slipper sätta upp nätverk.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _require_local_access(request: Request) -> None:
    """
    Släpper bara igenom anrop från loopback, om inte config.SETUP_ALLOW_REMOTE
    är satt. Setup-endpointsen skriver .env och sköter OAuth utan autentisering,
    så de ska inte gå att nå från nätverket som standard (se config.py).

    Körs automatiskt före varje endpoint i den här routern (se dependencies
    när routern skapas nedan), så ingen endpoint kan glömma kontrollen.

    Args:
        request: Anropet - används för att läsa avsändarens IP-adress.

    Raises:
        HTTPException 403: Om anropet kommer från en annan dator.
    """
    # Uttryckligen tillåtet (t.ex. i Docker) - släpp igenom allt.
    if config.SETUP_ALLOW_REMOTE:
        return
    # request.client kan saknas i vissa testmiljöer - räknas då som okänd.
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="Inställningsguiden är bara tillgänglig lokalt (från samma dator). "
            "Sätt SETUP_ALLOW_REMOTE=true i .env för att tillåta fjärråtkomst "
            "(gör det bara bakom en autentiserad omvänd proxy).",
        )


# dependencies=[...] gör att _require_local_access körs före VARJE endpoint
# i routern - en ny endpoint kan alltså aldrig av misstag bli oskyddad.
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
    "LOCAL_ASR_ENGINE",
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
    "AI_TITLE_PROMPT",
    "AI_DESCRIPTION_PROMPT",
    "AI_TEMPERATURE",
    "OLLAMA_NUM_CTX",
}

# Egna AI-prompter: standardprompten (att jämföra mot / återställa till).
# En sparad prompt som är identisk med standarden sparas som tom, så en
# framtida förbättring av standardprompten slår igenom automatiskt.
_PROMPT_DEFAULTS = {
    "AI_TITLE_PROMPT": ai_enrichment.TITLE_PROMPT_TEMPLATE,
    "AI_DESCRIPTION_PROMPT": ai_enrichment.DESCRIPTION_PROMPT_TEMPLATE,
}

# Nycklar vars värde aldrig skickas tillbaka i klartext till frontend.
_SECRET_KEYS = {"OPENAI_API_KEY", "SPREAKER_API_TOKEN", "SMTP_PASSWORD"}


class AuthorizeUrlRequest(BaseModel):
    """
    Indata för att bygga Spreakers godkännandelänk (steg 2 i guiden).
    """
    client_id: str
    redirect_uri: str = "http://localhost"


class ExchangeRequest(BaseModel):
    """
    Indata för att byta en auktoriseringskod mot en token (steg 3 i guiden).
    """
    client_id: str
    client_secret: str
    redirect_uri: str = "http://localhost"
    # Antingen själva koden, eller hela redirect-URL:en (t.ex.
    # "http://localhost/?state=...&code=ABC") - då plockas code ut åt användaren.
    code: str


class TokenRequest(BaseModel):
    """
    En befintlig Spreaker-token som ska verifieras.
    """
    token: str


class OpenAIKeyRequest(BaseModel):
    """
    En OpenAI-nyckel som ska verifieras.
    """
    api_key: str


class SaveRequest(BaseModel):
    """
    Inställningar som ska sparas: {"NYCKEL": "värde", ...}.
    """
    values: dict[str, str]


def _mask(value: str) -> str:
    """
    Maskerar en hemlighet men visar de sista tecknen, så användaren ser att NÅGOT är satt.

    Exempel: "sk-abc123xyz" -> "••••3xyz".

    Args:
        value: Hemligheten.

    Returns:
        Maskerad text, eller "" om inget är satt.
    """
    if not value:
        return ""
    # En mycket kort hemlighet skulle nästan avslöjas helt av sina fyra
    # sista tecken - visa då bara prickar.
    if len(value) <= 4:
        return "••••"
    return "••••" + value[-4:]


@router.get("/config")
async def get_config():
    """
    Nuvarande inställningar för guiden. Hemligheter maskeras (bara om de är
    satta + de sista tecknen), övriga värden skickas som de är.

    GET /api/setup/config - anropas när fliken Inställningar öppnas.

    Returns:
        En dict med ett fält per inställning. Hemligheter skickas bara som
        "är satt" (true/false) plus en maskerad version.
    """
    # Nycklarna är gemener med understreck och motsvarar .env-namnen, t.ex.
    # "ollama_model" = OLLAMA_MODEL. static/app.js fyller fälten härifrån.
    return {
        # Hemligheter: bara om de är satta och en maskerad version - aldrig
        # själva värdet.
        "openai_api_key_set": bool(config.OPENAI_API_KEY),
        "openai_api_key_masked": _mask(config.OPENAI_API_KEY),
        "use_local_whisper": config.USE_LOCAL_WHISPER,
        "local_whisper_model": config.LOCAL_WHISPER_MODEL,
        "whisper_device": config.WHISPER_DEVICE,
        "local_asr_engine": config.LOCAL_ASR_ENGINE,
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
        # Prompten som faktiskt används (egen eller standard) + standarden,
        # så fältet alltid visar något att utgå från och kan återställas.
        "ai_title_prompt": config.AI_TITLE_PROMPT.strip() or ai_enrichment.TITLE_PROMPT_TEMPLATE,
        "ai_title_prompt_default": ai_enrichment.TITLE_PROMPT_TEMPLATE,
        "ai_title_prompt_custom": bool(config.AI_TITLE_PROMPT.strip()),
        "ai_description_prompt": config.AI_DESCRIPTION_PROMPT.strip() or ai_enrichment.DESCRIPTION_PROMPT_TEMPLATE,
        "ai_description_prompt_default": ai_enrichment.DESCRIPTION_PROMPT_TEMPLATE,
        "ai_description_prompt_custom": bool(config.AI_DESCRIPTION_PROMPT.strip()),
        "ai_temperature": config.AI_TEMPERATURE,
        "ollama_num_ctx": config.OLLAMA_NUM_CTX,
    }


@router.post("/spreaker/authorize-url")
async def spreaker_authorize_url(req: AuthorizeUrlRequest):
    """
    Bygger auktoriseringslänken användaren öppnar för att få en kod (steg 2 i guiden).

    Args:
        req: Client ID och redirect-adress.

    Returns:
        {"url": ...} - adressen som öppnas i en ny flik.

    Raises:
        HTTPException 400: Om Client ID saknas.
    """
    if not req.client_id.strip():
        raise HTTPException(status_code=400, detail="Client ID är obligatoriskt.")
    url = spreaker_client.build_authorize_url(req.client_id.strip(), req.redirect_uri.strip())
    return {"url": url}


def _extract_code(code_or_url: str) -> str:
    """
    Tar antingen en rå kod eller hela redirect-URL:en och returnerar koden.

    Args:
        code_or_url: Det användaren klistrade in.

    Returns:
        Själva koden.

    Raises:
        HTTPException 400: Om en URL klistrades in men saknar code=.
    """
    value = code_or_url.strip()
    # Hela adressen klistrades in, t.ex. "http://localhost/?state=xyz&code=ABC".
    if value.startswith("http://") or value.startswith("https://"):
        # parse_qs ger listor, eftersom en parameter kan förekomma flera
        # gånger: {"code": ["ABC"], "state": ["xyz"]}.
        query = parse_qs(urlparse(value).query)
        found = query.get("code", [""])[0]
        if not found:
            raise HTTPException(status_code=400, detail="Hittade ingen 'code' i den inklistrade URL:en.")
        return found
    # Annars antas det vara själva koden.
    return value


@router.post("/spreaker/exchange")
async def spreaker_exchange(req: ExchangeRequest):
    """
    Byter auktoriseringskoden mot en token (steg 3), verifierar den och
    listar användarens shows (steg 4) - allt i ett anrop. Sparar INGET; det
    gör användaren själv via /save efter att ha valt show.

    Args:
        req: Client ID, Client Secret, redirect-adress och koden (eller hela
            adressen från adressfältet).

    Returns:
        {"token", "user", "shows"} - sidan fyller listan med shows och
        sparar token först när användaren klickar Spara.

    Raises:
        HTTPException 400: Om något fält saknas.
        HTTPException 502: Om Spreaker avvisar koden.
    """
    if not req.client_id.strip() or not req.client_secret.strip() or not req.code.strip():
        raise HTTPException(status_code=400, detail="Client ID, Client Secret och kod är obligatoriska.")
    code = _extract_code(req.code)
    try:
        # 1) Byt koden mot en token. Koden går bara att använda en gång och
        #    är bara giltig en kort stund.
        token = spreaker_client.exchange_oauth_code(
            req.client_id.strip(), req.client_secret.strip(), req.redirect_uri.strip(), code
        )
        # 2) Kontrollera att token fungerar och vems konto den gäller.
        user = spreaker_client.get_me(token)
        # 3) Lista kontots shows, så rätt show-id kan väljas i en lista.
        shows = spreaker_client.list_my_shows(token)
    except SpreakerUploadError as exc:
        # 502 = felet kom från Spreaker, inte från appen.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"token": token, "user": user, "shows": shows}


@router.post("/spreaker/verify")
async def spreaker_verify(req: TokenRequest):
    """
    Verifierar en redan befintlig token (för den som klistrar in en token
    direkt i stället för att köra OAuth-flödet) och listar dess shows.

    Args:
        req: Token som ska provas.

    Returns:
        {"user", "shows"} - samma lista med shows som vid OAuth-flödet.

    Raises:
        HTTPException 400: Om token saknas.
        HTTPException 502: Om Spreaker avvisar token.
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
    """
    Verifierar en OpenAI-nyckel genom ett lätt anrop mot /v1/models.

    Args:
        req: Nyckeln som ska provas.

    Returns:
        {"valid": true} om OpenAI godkände nyckeln.

    Raises:
        HTTPException 400: Om nyckeln saknas eller avvisas.
        HTTPException 502: Om OpenAI inte går att nå.
    """
    key = req.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="API-nyckel är obligatoriskt.")
    try:
        # Att lista modellerna är gratis och kräver en giltig nyckel - ett
        # bra sätt att prova nyckeln utan att det kostar något.
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

    POST /api/setup/save - anropas av alla Spara-knappar i fliken Inställningar.

    Args:
        req: De värden som ska sparas, som {"NYCKEL": "värde"}.

    Returns:
        Den nya konfigurationen (samma form som GET /config), så sidan kan
        visa exakt vad som nu gäller.

    Raises:
        HTTPException 400: Vid okänd nyckel eller ogiltigt värde (motor,
            temperatur, kontextfönster eller prompt utan {transcript}).
    """
    # 1) Bara kända nycklar får skrivas - annars skulle vem som helst med
    #    tillgång till sidan kunna lägga in godtyckliga rader i .env.
    unknown = set(req.values) - ALLOWED_KEYS
    if unknown:
        raise HTTPException(status_code=400, detail=f"Otillåtna nycklar: {', '.join(sorted(unknown))}")

    # 2) Rensa värdena. Ett tomt hemligt fält betyder "ändra inte" - sidan
    #    visar aldrig den sparade hemligheten, så fältet är tomt från början.
    updates: dict[str, str] = {}
    for key, value in req.values.items():
        if key in _SECRET_KEYS and value.strip() == "":
            continue  # lämna en redan sparad hemlighet orörd
        updates[key] = value.strip()

    # 3) Kontrollera värden som annars skulle ge fel först när de används.
    #    Tomma värden är alltid tillåtna och betyder "standardvärdet".
    if updates.get("LOCAL_ASR_ENGINE") and updates["LOCAL_ASR_ENGINE"] not in ("whisper", "pianissimo"):
        raise HTTPException(status_code=400, detail="Lokal motor måste vara whisper eller pianissimo.")
    if updates.get("AI_TEMPERATURE"):
        try:
            # Svenskt decimalkomma ("0,3") accepteras också.
            temperature = float(updates["AI_TEMPERATURE"].replace(",", "."))
        except ValueError:
            # Ogiltig text - ett värde utanför intervallet ger felet nedan.
            temperature = -1
        if not 0 <= temperature <= 2:
            raise HTTPException(status_code=400, detail="Temperaturen måste vara ett tal mellan 0 och 2.")
        # Sparas med punkt, som Python läser.
        updates["AI_TEMPERATURE"] = str(temperature)
    if updates.get("OLLAMA_NUM_CTX"):
        # Mindre än 2048 tokens räcker inte ens för prompten utan predikan.
        if not updates["OLLAMA_NUM_CTX"].isdigit() or int(updates["OLLAMA_NUM_CTX"]) < 2048:
            raise HTTPException(status_code=400, detail="Kontextfönstret måste vara ett heltal, minst 2048.")

    # 4) Prompterna: jämför med standarden och kräv {transcript}.
    for key, default in _PROMPT_DEFAULTS.items():
        if key not in updates:
            continue
        # Webbläsare skickar radbrytningar som \r\n - gör om till \n så att
        # jämförelsen med standardprompten fungerar.
        prompt = updates[key].replace("\r\n", "\n")
        if prompt == default.strip():
            prompt = ""  # samma som standarden -> spara tomt (= följ standarden)
        elif prompt and "{transcript}" not in prompt:
            label = "titel" if key == "AI_TITLE_PROMPT" else "beskrivning"
            raise HTTPException(
                status_code=400,
                detail=f"Prompten för {label} måste innehålla {{transcript}} - "
                "annars får AI:n aldrig se predikan.",
            )
        updates[key] = prompt

    # Inget att spara (t.ex. bara tomma hemliga fält) - lämna .env orörd.
    if not updates:
        return await get_config()

    # 5) Skriv .env och läs in den igen, så ändringarna gäller direkt.
    env_file.set_values(updates)
    config.reload()
    return await get_config()
