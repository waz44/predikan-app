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
import re
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
    # Fraser ur den tidigare standardprompten (kan finnas i gamla svar)
    "lyfter fram en central fråga",
    "följt av en punktlista",
    "du redigerar en beskrivning",
    "automatiskt transkriberat med whisper",
    "svara endast med beskrivningen",
    # Fraser och mallrader ur den nuvarande standardprompten
    "[2-3 meningars inledning]",
    "[punkt]",
    "[2-3 meningar]",
    "väcker nyfikenhet genom att lyfta",
    "svara bara med beskrivningen",
    "transkriptet är automatiskt transkriberat",
]

# Max antal tecken av transkriptet som skickas med. Uppmätt ca 2,7 tecken
# per token för svenska med llama3.1, så 36 000 tecken är ca 13 300 tokens
# och ryms med prompt och svar i config.OLLAMA_NUM_CTX = 16384. Längre
# transkript kortas i MITTEN, inte i slutet (se _trim_transcript) -
# avslutningen är ofta predikans poäng.
TRANSCRIPT_CHAR_LIMIT = 36000
_TRIM_HEAD_SHARE = 0.7

# Temperatur för ETT nytt försök när beskrivningen ser ut att ha läckt
# prompttext. Med den vanliga låga temperaturen skulle omförsöket ge i stort
# sett samma svar igen.
RETRY_TEMPERATURE = 0.7

# Alla tre standardprompterna BÖRJAR med exakt samma text (_SHARED_PREFIX:
# inledning + talare + transkript) och har uppgiften och reglerna sist:
#
# - Ollama återanvänder det den redan läst när nästa prompt börjar likadant.
#   Då behöver bara första anropet (titeln) läsa hela predikan - det tar
#   många minuter på CPU - medan beskrivningen och taggarna bara läser sin
#   egen uppgift. Ändra därför aldrig början av EN prompt utan de andra.
# - Lokala modeller följer bäst det som står närmast slutet av en lång
#   prompt, alltså uppgiften och reglerna.
#
# Formatet för beskrivningen visas som en MALL i stället för som numrerade
# instruktioner - de numrerade instruktionerna var just det som modellen
# ibland skrev av rakt in i svaret (se _PROMPT_LEAK_MARKERS).
_SHARED_PREFIX = """Här är ett transkript av en predikan från en kristen predikopodd.

Talare: {speaker}

Transkriptet är automatiskt transkriberat och innehåller felhörningar, talspråk och ord som blivit fel.
\"\"\"
{transcript}
\"\"\"

"""

TITLE_PROMPT_TEMPLATE = _SHARED_PREFIX + """Uppgift: skriv titeln till avsnittet.

Regler för titeln:
- Skriv den i formen {speaker}: Titel
- Själva titeln (efter kolon) är 2-7 ord på svenska och fångar predikans huvudbudskap - gärna en formulering eller bild som predikanten själv använder.
- Var konkret och specifik. Undvik allmänna titlar som "En predikan om tro" eller "Guds kärlek".
- Svensk stavning: stor bokstav bara i första ordet och i namn.
- Rätta uppenbara felhörningar av namn och bibelböcker.
- Bortse från podcastens inledning och avslutning (t.ex. "Du lyssnar på en podcast från ...") och från praktiska meddelanden.
- Inga citattecken, ingen punkt på slutet, ingen förklaring.

Om transkriptet är för kort, tomt eller obegripligt för att avgöra vad predikan handlar om, svara bara: {speaker}

Svara med en enda rad: titeln."""

DESCRIPTION_PROMPT_TEMPLATE = _SHARED_PREFIX + """Uppgift: skriv avsnittsbeskrivningen. Den visas för lyssnare i podcastappar.

Skriv beskrivningen på svenska i exakt det här formatet:

[2-3 meningars inledning]

Viktiga punkter:
- [punkt]
- [punkt]
- [punkt]

Sammanfattning:
[2-3 meningar]

Regler:
- Inledningen väcker nyfikenhet genom att lyfta en fråga, ett dilemma eller ett behov som predikan tar upp. Varm och saklig ton, inte säljig. Börja inte med "Predikan handlar om".
- Viktiga punkter: 3-5 punkter med predikans viktigaste tankar, i den ordning de kommer. Varje punkt är en hel mening. Nämn bibelställen bara om predikanten tydligt hänvisar till dem.
- Sammanfattningen knyter ihop helheten och säger vad lyssnaren kan ta med sig.
- Håll dig till det som faktiskt sägs. Hitta inte på bibelord, citat, berättelser eller fakta.
- Rätta uppenbara felhörningar av namn och bibelböcker (t.ex. "Thessaloniki brevet" blir "Thessalonikerbrevet"), men gissa inte när du är osäker.
- Bortse från podcastens inledning och avslutning (t.ex. "Du lyssnar på en podcast från ...") och från praktiska meddelanden.
- Upprepa inte samma tanke i flera punkter eller delar.
- Ren text: ingen markdown (inga ** eller #), inga hakparenteser, inga citattecken runt svaret.

Om transkriptet är för kort, tomt eller obegripligt för att sammanfattas tillförlitligt, svara bara med exakt denna text:
{fallback_text}

Svara bara med beskrivningen och börja direkt med inledningen."""

TAGS_PROMPT_TEMPLATE = _SHARED_PREFIX + """Uppgift: välj taggar till avsnittet.

Tillåtna taggar:
{tag_list}

Regler:
- Välj 1-3 taggar ur listan som bäst beskriver predikans huvudtema. Hellre en träffande tagg än tre halvbra.
- Skriv taggarna exakt som i listan. Hitta inte på egna taggar.
- Bortse från podcastens inledning och avslutning och från praktiska meddelanden.
- Om transkriptet är för kort eller obegripligt för att bedöma, svara med en tom rad.

Svara med en enda rad: taggarna separerade med kommatecken, t.ex.
Tro & Tvivel, Guds karaktär"""


# Standardprompterna ovan kan ersättas med egna via AI_TITLE_PROMPT /
# AI_DESCRIPTION_PROMPT i .env (eller fliken Inställningar). Platshållarna
# nedan fylls i; övriga {klammerparenteser} i en egen prompt lämnas orörda.
TITLE_PLACEHOLDERS = ("speaker", "transcript")
DESCRIPTION_PLACEHOLDERS = ("speaker", "transcript", "fallback_text")


def _title_template() -> str:
    return config.AI_TITLE_PROMPT.strip() or TITLE_PROMPT_TEMPLATE


def _description_template() -> str:
    return config.AI_DESCRIPTION_PROMPT.strip() or DESCRIPTION_PROMPT_TEMPLATE


def _fill(template: str, **values: str) -> str:
    """
    Fyller i {namn}-platshållarna utan str.format(), så en egen prompt med
    andra klammerparenteser (t.ex. ett JSON-exempel) inte kraschar.
    """
    for name, value in values.items():
        template = template.replace("{" + name + "}", value)
    return template


def _trim_transcript(transcript: str) -> str:
    """
    Kortar ett för långt transkript i mitten: behåller början (där temat
    och bibeltexten brukar presenteras) och slutet (där predikan landar),
    i stället för att bara klippa bort slutet.
    """
    if len(transcript) <= TRANSCRIPT_CHAR_LIMIT:
        return transcript
    head = int(TRANSCRIPT_CHAR_LIMIT * _TRIM_HEAD_SHARE)
    tail = TRANSCRIPT_CHAR_LIMIT - head
    return f"{transcript[:head]}\n\n[...]\n\n{transcript[-tail:]}"


def _clean_title(raw: str, speaker: str, enforce_speaker: bool) -> str:
    """
    Städar modellens svar: första icke-tomma raden, utan "Titel:"-prefix,
    citattecken eller avslutande punkt. Med standardprompten säkras också
    formen "Talare: Titel" (samma form som alla tidigare avsnitt).
    """
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if line.lower().startswith("titel:"):
        line = line[len("titel:"):].strip()
    line = line.strip("\"'*“”„ ").rstrip(".").strip()
    if not line:
        return speaker
    if enforce_speaker and speaker and not line.lower().startswith(speaker.lower()):
        line = f"{speaker}: {line}"
    return line


_LABEL_LINE = re.compile(r"^\s*(\[[^\]\n]{1,40}\]|inledning:?)\s*$", re.IGNORECASE)


def _clean_description(raw: str) -> str:
    """
    Tar bort rester av formatmallen som modellen ibland skriver ut som egna
    rader (t.ex. "[Inledning]" först i svaret - sett med llama3.1) och
    markdown-fetstil, som inte visas som fetstil i podcastappar.
    """
    lines = [line for line in raw.replace("**", "").splitlines() if not _LABEL_LINE.match(line)]
    return "\n".join(lines).strip()


def generate_title(transcript: str, speaker: str, base_name: str = "") -> str:
    """Genererar en titel som alltid innehåller talarens namn."""
    prompt = _fill(_title_template(), speaker=speaker, transcript=_trim_transcript(transcript))
    raw = _call_ai(prompt, debug_tag="title", base_name=base_name)
    return _clean_title(raw, speaker, enforce_speaker=not config.AI_TITLE_PROMPT.strip())


def generate_description(transcript: str, speaker: str, base_name: str = "") -> str:
    """
    Genererar en strukturerad beskrivning (inledning/punkter/sammanfattning).
    Om svaret ser ut att ha läckt in delar av själva PROMPTEN (se
    _looks_like_prompt_leak) görs ETT nytt försök med högre temperatur
    (RETRY_TEMPERATURE), så att omförsöket faktiskt kan bli annorlunda.
    Misslyckas det också, faller det tillbaka på samma
    QUALITY_FALLBACK_TEXT som används för för korta/obegripliga
    transkript, istället för att publicera en trasig beskrivning.

    Läckkontrollen görs bara med standardprompten: den letar efter fraser
    ur just den prompten och förutsätter dess struktur (inledning före
    "Viktiga punkter:"), vilket inte behöver gälla för en egen prompt.
    """
    prompt = _fill(
        _description_template(),
        speaker=speaker,
        transcript=_trim_transcript(transcript),
        fallback_text=QUALITY_FALLBACK_TEXT,
    )
    raw = _call_ai(prompt, debug_tag="description", base_name=base_name).strip()
    if config.AI_DESCRIPTION_PROMPT.strip():
        return raw  # egen prompt: svaret lämnas som modellen skrev det

    raw = _clean_description(raw)
    if _looks_like_prompt_leak(raw):
        raw = _clean_description(
            _call_ai(prompt, debug_tag="description-retry", base_name=base_name, temperature=RETRY_TEMPERATURE)
        )
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


def generate_tags(transcript: str, speaker: str = "", base_name: str = "") -> list[str]:
    """
    Genererar 1-3 taggar, alltid validerade mot ALLOWED_TAGS. Talaren
    behövs bara för att prompten ska börja exakt som titel-/beskrivnings-
    prompten (se _SHARED_PREFIX), så Ollama kan återanvända det den läst.
    """
    prompt = _fill(
        TAGS_PROMPT_TEMPLATE,
        speaker=speaker,
        transcript=_trim_transcript(transcript),
        tag_list="\n".join(f"- {tag}" for tag in ALLOWED_TAGS),
    )
    raw = _call_ai(prompt, debug_tag="tags", base_name=base_name)
    # Tåla att modellen svarar med en tagg per rad (som listan i prompten).
    candidates = [t.strip().lstrip("-• ").strip() for t in raw.replace("\n", ",").split(",")]
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


def _call_ai(prompt: str, debug_tag: str, base_name: str = "", temperature: float | None = None) -> str:
    """
    Skickar prompten till den konfigurerade AI-leverantören och loggar råsvaret.
    Temperaturen är config.AI_TEMPERATURE om inget annat anges.
    """
    if temperature is None:
        temperature = config.AI_TEMPERATURE
    if config.AI_PROVIDER == "ollama":
        raw = _call_ollama(prompt, temperature)
    else:
        raw = _call_openai(prompt, temperature)
    _save_debug(raw, debug_tag, base_name)
    return raw


def _call_openai(prompt: str, temperature: float) -> str:
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
        temperature=temperature,
        max_tokens=700,
    )
    return response.choices[0].message.content or ""


def _call_ollama(prompt: str, temperature: float) -> str:
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
                "options": {
                    "temperature": temperature,
                    # Utan detta används Ollamas egna, för lilla kontextfönster
                    # och början av en lång prompt klipps tyst bort.
                    "num_ctx": config.OLLAMA_NUM_CTX,
                    "num_predict": 1024,  # tak för svarets längd
                },
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
