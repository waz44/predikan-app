"""
Modul: ai_enrichment
Använder en AI-modell för att, utifrån transkriptet och talarens namn, generera:
- En slagkraftig titel (endast om användaren lämnat fältet tomt)
- En sammanfattande beskrivning / predikans kärna (endast om tomt)
- Förslag på taggar/kategorisering

Stöder två lägen (styrs av config.AI_PROVIDER):
- "openai": GPT via OpenAI API (kräver OPENAI_API_KEY)
- "ollama": en lokal modell via Ollama, t.ex. Llama 3.1 - helt offline,
  ingen data lämnar datorn, ingen API-nyckel behövs.
"""
import json
import re
import logging
from pathlib import Path
import requests
import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Du är en expert på redigering och sammanfattning av kristen undervisning och predikningar. 
Den bifogade texten är automatiskt transkriberad från tal med Whisper. Den kan därför innehålla felhörda ord, talspråksord (som 'liksom', 'ööh', 'typ') och sakna vettig meningsbyggnad.

INSTRUKTIONER:

1. ANALYS AV KÄLLAN
   - Identifiera kärnan och de viktigaste poängerna i predikan
   - Ignorera uppenbara felhörningar och talspråkligt slask
   - Reparera sammanhanget för att återskapa talarens avsikt
   - Notera om transkriptet är fragmentariskt eller svårt att tolka

2. STRUKTUR FÖR DESCRIPTION (på SVENSKA, med radbrytningar enligt nedan)
   
   [SEKTION 1: HOOK - 2-3 meningar]
   Lyfta fram en central fråga, ett dilemma eller ett mänskligt behov som predikan tar upp.
   Syftet: att göra läsaren nyfiken på att lyssna, utan säljig ton.
   
   [SEKTION 2: SAMMANFATTNING - 2-3 meningar]
   Sammanfatta helheten i predikan: vilken huvudpunkt gör talarens och varför är den viktig?
   
   [SEKTION 3: LÄRDOMAR - punktlista med 3-5 punkter]
   De viktigaste lärdomarna, bibelställena eller diskussionsämnena.
   Format: "- [Tema]: [Kort beskrivning, 1 rad]"

3. TAGGVAL
   Välj EXAKT 1, 2 eller 3 tags från denna lista (inga egna taggar, ingen omskrivning):
   - Tro & Tvivel
   - Relationer & Familj
   - Bibeln & Teologi
   - Livskris & Hopp
   - Vardagskristendom
   - Lärjungaskap & Efterföljelse
   - Församling & Gemenskap
   - Högtider & Kyrkoåret
   - Guds karaktär

4. TITEL
   Format: "[Talare]: [Kort rubrik, max 8 ord]"
   Exempel: "Anders Fsjord: Vägen ur tvivlet"

FELHANTERING:

Om transkriptet uppfyller NÅGOT av följande kriterier, lägg predikan i kategorin "OSÄKER KVALITET":
- Mindre än 200 ord sammanlagt
- Innehåller stora luckor (flera sekunder tystnad, "[OKÄND]" eller liknande)
- Samma sak upprepas flera gånger utan ny information
- Talaren är nästan helt obegriplig
- Transkriptet verkar vara från mitten av predikan (saknar introduktion/avslut)

Om "OSÄKER KVALITET": returnera ENDAST detta JSON:
{
  "title": "[Talare]",
  "description": "Texten kunde inte sammanfattas på ett tillförlitligt sätt.",
  "tags": [],
  "quality_warning": "OSÄKER_KVALITET"
}

NORMALT FALL: returnera detta JSON:
{
  "title": "...",
  "description": "...",
  "tags": ["...", "..."],
  "quality_warning": null
}

FINPUTPOLERING:
- Behåll en professionell men lättläst ton
- Hitta inte på fakta, bibelord eller citat som inte nämns i texten
- Korta ner flösiga meningar till en tydlig poäng
- Använd målgruppsanpassad språk (inte för akademiskt)

Svara ENDAST med ett giltigt JSON-objekt (ingen extra text, inga markdown-taggar,
inga inledande eller avslutande kommentarer)."""


def enrich_metadata(
    transcript: str,
    speaker: str,
    need_title: bool,
    need_description: bool,
) -> dict:
    """
    Genererar titel/beskrivning/taggar utifrån transkriptet, med den AI-leverantör
    som är konfigurerad i .env (config.AI_PROVIDER).
    
    Returns:
        Dict med keys: title, description, tags, quality_flag
        quality_flag är True om OSÄKER_KVALITET detekterades.
    """
    # Öka begränsningen så längre transkript kan användas (modellens token-budget avgör hur mycket som verkligen används)
    truncated_transcript = transcript[:30000]
    # Tydliggör för modellen att description måste innehålla alla definierade sektioner,
    # och be om minst ~120 ord om du vill ha mer text.
    user_prompt = f"""Talare: {speaker}

Transkript av predikan:
\"\"\"
{truncated_transcript}
\"\"\"

Generera titel, beskrivning (inkl. SEKTION 1/2/3 enligt instruktionerna i systemprompten) och taggar enligt instruktionerna.
OBS: Beskrivningen ska innehålla alla sektioner och vara minst 100-150 ord om möjligt.
Svara endast med ett giltigt JSON-objekt enligt systemprompten."""

    if config.AI_PROVIDER == "ollama":
        data = _enrich_ollama(user_prompt)
    else:
        data = _enrich_openai(user_prompt)

    # Hantering av quality_warning
    if data.get("quality_warning") == "OSÄKER_KVALITET":
        logger.warning(f"AI-berikning: OSÄKER KVALITET för talare '{speaker}'")
        return {
            "title": speaker,
            "description": "Texten kunde inte sammanfattas på ett tillförlitligt sätt.",
            "tags": [],
            "quality_flag": True
        }

    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    tags = data.get("tags", [])

    # Validera tags: säkerställ att det är en lista
    if not isinstance(tags, list):
        tags = []

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "quality_flag": False
    }

def _enrich_openai(user_prompt: str) -> dict:
    from openai import OpenAI
    import time

    if not config.OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY saknas i .env - krävs för AI-berikning med OpenAI. "
            "Sätt AI_PROVIDER=ollama i .env om du vill köra helt lokalt istället."
        )

    client = OpenAI(api_key=config.OPENAI_API_KEY)

    # Sätt max_tokens så response inte kapas av servern på för få token
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=1500,
    )

    raw = response.choices[0].message.content

    # Spara rått AI-svar för felsökning
    try:
        ts = int(time.time())
        p = config.PROCESSED_DIR / f"last_ai_openai_raw_{ts}.txt"
        p.write_text(raw, encoding="utf-8")
        logger.debug(f"Sparade OpenAI rått svar: {p}")
    except Exception as e:
        logger.warning(f"Kunde inte spara OpenAI råsvar: {e}")

    # Försök parsa JSON från content
    return _parse_json_loose(raw)

def _enrich_ollama(user_prompt: str) -> dict:
    """
    Anropar en lokalt körande Ollama-server (https://ollama.com).
    Kräver att Ollama är installerat och igång, samt att modellen
    (config.OLLAMA_MODEL) är nedladdad via `ollama pull <modell>`.
    """
    import time
    try:
        response = requests.post(
            f"{config.OLLAMA_HOST}/api/chat",
            json={
                "model": config.OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "format": "json",
                # Lägg till max_tokens i options så lokala modeller inte trimmar svaret för tidigt
                "options": {"temperature": 0.7, "num_predict": 1500},
            },
            timeout=300,
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

    # Vissa lokala modeller returnerar sitt meddelande i message.content
    content = response.json().get("message", {}).get("content", "")

    # Spara rått AI-svar för felsökning
    try:
        ts = int(time.time())
        p = config.PROCESSED_DIR / f"last_ai_ollama_raw_{ts}.txt"
        p.write_text(content, encoding="utf-8")
        logger.debug(f"Sparade Ollama råsvar: {p}")
    except Exception as e:
        logger.warning(f"Kunde inte spara Ollama råsvar: {e}")

    return _parse_json_loose(content)


def _parse_json_loose(content: str) -> dict:
    """
    Lokala modeller lyder inte alltid JSON-formatet perfekt (kan t.ex. lägga
    till ```json-block runt svaret eller ha trailing kommatecken).
    
    Denna funktion försöker parsa JSON på flera sätt:
    1. Direkt JSON-parsning
    2. Hitta JSON-objekt inne i markdown-kodblock (```json...```)
    3. Hitta första {...} block med regex
    4. Försöka åtgärda vanliga JSON-fel (trailing kommatecken, osv)
    
    Raises:
        RuntimeError: Om JSON inte kan parsas efter alla försök
    """
    # Försök 1: Direkt parsning
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Försök 2: Sök efter ```json ... ``` block
    json_block_match = re.search(r'```(?:json)?\s*\n(.*?)\n```', content, re.DOTALL)
    if json_block_match:
        try:
            json_str = json_block_match.group(1).strip()
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # Försök 3: Hitta första {...} block (greedy match)
    brace_match = re.search(r'\{(?:[^{}]|(?:\{[^{}]*\}))*\}', content, re.DOTALL)
    if brace_match:
        try:
            json_str = brace_match.group(0)
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # Försök 4: Hitta första {...} block (non-greedy, enklare regex)
    simple_match = re.search(r'(\{.+?\})', content, re.DOTALL)
    if simple_match:
        try:
            json_str = simple_match.group(1)
            # Försök åtgärda vanliga problem
            # Ta bort trailing kommatecken innan } eller ]
            json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # Försök 5: Mer aggressiv reparation - försök hitta JSON-struktur
    try:
        # Ta bort newlines och extra whitespace inuti strings
        cleaned = re.sub(r'\n\s*', ' ', content)
        # Ta bort trailing kommatecken
        cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
        
        # Hitta {...}
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except json.JSONDecodeError:
        pass

    # Alla försök misslyckades
    logger.error(f"Kunde inte parsa AI-svar som JSON. Rått innehål:\n{content[:500]}")
    raise RuntimeError(
        "Kunde inte tolka AI-svaret som JSON efter flera försök. "
        "Kontrollera filerna last_ai_*_raw_*.txt i processed/ för råsvaret. "
        "Möjliga orsaker: modellen returnerade inte giltigt JSON, eller "
        "har ändrat sitt svarsformat."
    )


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
    logger.debug(f"Sparade enrichment-resultat: {json_path}")
    return json_path
