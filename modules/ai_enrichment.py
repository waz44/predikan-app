"""
Modul: ai_enrichment
Använder en AI-modell för att, utifrån transkriptet och talarens namn, generera:
- En slagkraftig titel (endast om användaren lämnat fältet tomt)
- En sammanfattande beskrivning / predikans kärna (endast om tomt)
- Förslag på taggar (alltid, ingen manuell motsvarighet finns i formuläret)

Detta görs som TRE separata, oberoende anrop (en per fält) istället för ett
enda JSON-anrop. Två skäl:

1. Kontroll/prestanda: appen anropar bara AI:n för fält som faktiskt saknas
   (t.ex. om användaren redan skrivit en titel hoppas det anropet över helt).
2. Tillförlitlighet: varje anrop ber om REN TEXT, inte JSON. Detta undviker
   en hel klass av buggar där lokala modeller (via Ollama) råkar svara med
   översatta JSON-nycklar (t.ex. "titel"/"beskrivning"/"taggar" istället för
   "title"/"description"/"tags") - koden hittar då aldrig rätt fält och allt
   blir tomt, trots att modellen egentligen genererade fullt användbart
   innehåll. Enkel text har inga nycklar som kan misstolkas eller översättas.

Stöder två lägen (styrs av config.AI_PROVIDER):
- "openai": GPT via OpenAI API (kräver OPENAI_API_KEY)
- "ollama": en lokal modell via Ollama, t.ex. Llama 3.1 - helt offline,
  ingen data lämnar datorn, ingen API-nyckel behövs.
"""
import json
import time
from pathlib import Path

import requests

import config

# Den enda tillåtna uppsättningen taggar. AI-modeller (särskilt lokala via
# Ollama) följer inte alltid instruktioner om taggval perfekt, så vi
# validerar/filtrerar alltid svaret mot denna lista innan det används.
ALLOWED_TAGS = [
    "Tro & Tvivel",
    "Relationer & Familj",
    "Bibeln & Teologi",
    "Livskris & Hopp",
    "Vardagskristendom",
    "Lärjungaskap & Efterföljelse",
    "Församling & Gemenskap",
    "Högtider & Kyrkoåret",
    "Guds karaktär",
]

QUALITY_FALLBACK_TEXT = "Texten kunde inte sammanfattas på ett tillförlitligt sätt."

# Skyddsnät mot två varianter av samma kända svaghet hos (särskilt
# mindre, lokala) AI-modeller: de missar ibland att skriva STEG 1
# (inledningen) i den 3-delade beskrivningsstrukturen (se
# DESCRIPTION_PROMPT_TEMPLATE) - antingen genom att hoppa över den helt
# och börja direkt på "Viktiga punkter:" (bekräftat på sex redan
# publicerade avsnitt), eller genom att råka svara med en omskrivning av
# SJÄLVA INSTRUKTIONEN istället för att följa den, t.ex. "en inledning på
# 2-3 meningar som lyfter fram..." (bekräftat på tre andra avsnitt). Båda
# fallen fångas av _looks_like_prompt_leak/generate_description().
_PROMPT_LEAK_MARKERS = [
    "lyfter fram en central fråga",
    "följt av en punktlista",
    "du redigerar en beskrivning",
    "automatiskt transkriberat med whisper",
    "svara endast med beskrivningen",
]

TRANSCRIPT_CHAR_LIMIT = 30000  # modellens token-budget avgör hur mycket som verkligen används

TITLE_PROMPT_TEMPLATE = """Du redigerar metadata för en kristen predikan-podcast.

Talare: {speaker}

Transkript (automatiskt transkriberat med Whisper - kan innehålla
felhörningar, talspråksord och sakna vettig meningsbyggnad):
\"\"\"
{transcript}
\"\"\"

Skriv EN kort, slagkraftig titel (max 8 ord) på svenska som fångar
predikans kärnbudskap. Titeln ska innehålla talarens namn på ett naturligt
sätt, t.ex. "{speaker}: Kärnbudskapet" eller "Kärnbudskapet - {speaker}".

Om transkriptet är för kort, tomt, upprepande eller obegripligt för att
kunna sammanfattas på ett tillförlitligt sätt (t.ex. under 200 ord, mest
tystnad/felhörningar, eller ett fragment utan sammanhang), svara ENDAST
med talarens namn: {speaker}

Svara ENDAST med titeln själv - ingen extra text, inga citattecken, ingen
förklaring, ingen rubrik."""

DESCRIPTION_PROMPT_TEMPLATE = """Du redigerar en beskrivning för en kristen predikan-podcast.

Talare: {speaker}

Transkript (automatiskt transkriberat med Whisper - kan innehålla
felhörningar, talspråksord och sakna vettig meningsbyggnad):
\"\"\"
{transcript}
\"\"\"

Skriv en beskrivning på SVENSKA i exakt denna struktur, med radbrytning
mellan delarna:

1. En inledning på 2-3 meningar som lyfter fram en central fråga, ett
   dilemma eller ett mänskligt behov som predikan tar upp - syftet är att
   göra läsaren nyfiken på att lyssna, utan att tonen blir säljig eller
   överdriven.
2. Rubriken "Viktiga punkter:" följt av en punktlista (varje punkt på egen
   rad, inledd med "- ") med de viktigaste lärdomarna, bibelställena
   och/eller diskussionsämnena från predikan.
3. Rubriken "Sammanfattning:" följt av 2-3 meningar som sammanfattar
   helheten.

Hitta inte på bibelord, citat eller fakta som inte förekommer i
transkriptet.

SÄRSKILT UNDANTAG: Om transkriptet är för kort, fragmentariskt, upprepande
eller obegripligt för att kunna sammanfattas på ett tillförlitligt sätt,
svara ENDAST med exakt denna text och inget annat:
{fallback_text}

Svara ENDAST med beskrivningen (eller undantagstexten ovan) - ingen egen
rubrik, inga citattecken, ingen kommentar före eller efter.

VIKTIGT: Skriv den FAKTISKA texten om predikan. Upprepa ALDRIG
instruktionerna ovan (t.ex. "en inledning på 2-3 meningar som lyfter
fram en central fråga...") som en del av svaret - det är en beskrivning
av vad du ska skriva, inte något du ska skriva av. Hoppa inte heller
över steg 1 (inledningen) - svaret ska ALLTID börja med den, aldrig
direkt med rubriken "Viktiga punkter:"."""

TAGS_PROMPT_TEMPLATE = """Här är transkriptet av en kristen predikan (automatiskt
transkriberat med Whisper - kan innehålla felhörningar):
\"\"\"
{transcript}
\"\"\"

Välj 1-3 taggar som bäst beskriver innehållet i predikan - ENDAST från
denna lista, återge dem exakt som de står här (hitta inte på egna taggar
och skriv inte om dem):
{tag_list}

Svara ENDAST med de valda taggarna separerade med kommatecken, exakt som
de står i listan ovan - ingen extra text, inga citattecken, ingen
numrering, ingen rubrik. Exempel på svarsformat: Tro & Tvivel, Guds karaktär

Om transkriptet är för kort, rörigt eller obegripligt för att kunna
bedöma några taggar tillförlitligt, svara med en tom rad istället för att
gissa."""


def generate_title(transcript: str, speaker: str, base_name: str = "") -> str:
    """Genererar en titel som alltid innehåller talarens namn."""
    prompt = TITLE_PROMPT_TEMPLATE.format(
        speaker=speaker, transcript=transcript[:TRANSCRIPT_CHAR_LIMIT]
    )
    raw = _call_ai(prompt, debug_tag="title", base_name=base_name)
    title = raw.strip().strip('"').strip("'").strip()
    return title or speaker


def generate_description(transcript: str, speaker: str, base_name: str = "") -> str:
    """
    Genererar en strukturerad beskrivning (inledning/punkter/sammanfattning).
    Om svaret ser ut att ha läckt in delar av själva PROMPTEN (se
    _looks_like_prompt_leak) görs ETT nytt försök (modellen är inte
    deterministisk - temperature 0.7 - så ett omförsök brukar räcka).
    Misslyckas det också, faller det tillbaka på samma
    QUALITY_FALLBACK_TEXT som används för för korta/obegripliga
    transkript, istället för att publicera en trasig beskrivning.
    """
    prompt = DESCRIPTION_PROMPT_TEMPLATE.format(
        speaker=speaker,
        transcript=transcript[:TRANSCRIPT_CHAR_LIMIT],
        fallback_text=QUALITY_FALLBACK_TEXT,
    )
    raw = _call_ai(prompt, debug_tag="description", base_name=base_name).strip()
    if _looks_like_prompt_leak(raw):
        raw = _call_ai(prompt, debug_tag="description-retry", base_name=base_name).strip()
        if _looks_like_prompt_leak(raw):
            return QUALITY_FALLBACK_TEXT
    return raw


def _looks_like_prompt_leak(text: str) -> bool:
    """
    Se _PROMPT_LEAK_MARKERS för bakgrunden - fångar BÅDA kända varianterna
    av att modellen missar att skriva inledningen (steg 1): antingen att
    den svarar med en omskrivning av själva instruktionen (substrängs-
    matchning, case-insensitive), eller att den hoppar över steg 1 helt
    och börjar direkt på steg 2 (rubriken "Viktiga punkter:").
    """
    lowered = text.lower().strip()
    if lowered.startswith("viktiga punkter"):
        return True
    return any(marker in lowered for marker in _PROMPT_LEAK_MARKERS)


def generate_tags(transcript: str, base_name: str = "") -> list[str]:
    """Genererar 1-3 taggar, alltid validerade mot ALLOWED_TAGS."""
    prompt = TAGS_PROMPT_TEMPLATE.format(
        transcript=transcript[:TRANSCRIPT_CHAR_LIMIT],
        tag_list=", ".join(ALLOWED_TAGS),
    )
    raw = _call_ai(prompt, debug_tag="tags", base_name=base_name)
    candidates = [t.strip() for t in raw.split(",")]
    return _validate_tags(candidates)


def _validate_tags(tags) -> list[str]:
    """
    Filtrerar bort taggar som inte finns i ALLOWED_TAGS (case-insensitive
    matchning). Skyddsnät oavsett hur väl modellen följer prompten.
    """
    allowed_lookup = {t.lower(): t for t in ALLOWED_TAGS}
    valid: list[str] = []
    for tag in tags or []:
        if not isinstance(tag, str):
            continue
        match = allowed_lookup.get(tag.strip().lower())
        if match and match not in valid:
            valid.append(match)
    return valid


def _call_ai(prompt: str, debug_tag: str, base_name: str = "") -> str:
    """Skickar prompten till den konfigurerade AI-leverantören och loggar råsvaret."""
    if config.AI_PROVIDER == "ollama":
        raw = _call_ollama(prompt)
    else:
        raw = _call_openai(prompt)
    _save_debug(raw, debug_tag, base_name)
    return raw


def _call_openai(prompt: str) -> str:
    from openai import OpenAI

    if not config.OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY saknas i .env - krävs för AI-berikning med OpenAI. "
            "Sätt AI_PROVIDER=ollama i .env om du vill köra helt lokalt istället."
        )

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=700,
    )
    return response.choices[0].message.content or ""


def _call_ollama(prompt: str) -> str:
    """
    Anropar en lokalt körande Ollama-server (https://ollama.com).
    Kräver att Ollama är installerat och igång, samt att modellen
    (config.OLLAMA_MODEL) är nedladdad via `ollama pull <modell>`.
    """
    try:
        response = requests.post(
            f"{config.OLLAMA_HOST}/api/chat",
            json={
                "model": config.OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.7},
            },
            timeout=3600,  # lokala modeller kan vara långsamma på CPU, ge gott om marginal
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            "Kunde inte ansluta till Ollama. Kontrollera att Ollama är "
            "installerat och igång (kommandot 'ollama serve' eller att "
            "Ollama-appen körs), och att modellen är nedladdad med "
            f"'ollama pull {config.OLLAMA_MODEL}'."
        ) from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Ollama-anrop misslyckades ({response.status_code}): {response.text}"
        )

    return response.json().get("message", {}).get("content", "")


def _save_debug(raw: str, tag: str, base_name: str = "") -> None:
    """
    Sparar rått AI-svar per fält för felsökning (skriv aldrig fel om detta
    misslyckas). Namnges enligt samma "<bas>-..."-standard som övriga filer
    i processed/ (se app.py:_run_processing_job), så filerna hör ihop med
    rätt predikan och städas bort automatiskt av storage_cleanup när
    MAX_STORED_EPISODES är satt.
    """
    try:
        prefix = base_name or f"debug-{int(time.time())}"
        p = config.PROCESSED_DIR / f"{prefix}-ai-{config.AI_PROVIDER}-{tag}.txt"
        p.write_text(raw, encoding="utf-8")
    except Exception:
        pass


def save_enrichment_result(enrichment_data: dict, json_path: Path) -> Path:
    """
    Sparar AI-berikningen (titel, beskrivning, taggar) till en JSON-fil.

    Args:
        enrichment_data: Ordboken med title, description, tags, etc.
        json_path: Sökväg där JSON-filen ska sparas

    Returns:
        Sökvägen till den sparade JSON-filen.
    """
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(enrichment_data, f, ensure_ascii=False, indent=2)
    return json_path
