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
import requests
import config

SYSTEM_PROMPT = """Du är en expert på redigering och sammanfattning av kristen undervisning och predikningar. 
Den bifogade texten är automatiskt transkriberad från tal med Whisper. Den kan därför innehålla felhörda ord, talspråksord (som 'liksom', 'ööh', 'typ') och sakna vettig meningsbyggnad.

Gör följande:
1. Identifiera kärnan och de viktigaste poängerna i predikan.
2. Ignorera uppenbara felhörningar och talspråkligt slask. Reparera sammanhanget.
3. Skapa en description på SVENSKA med följande struktur och med radbrytningar mellan delarna:
   * 2-3 meningar som lyfter fram en central fråga, ett dilemma eller ett mänskligt behov som predikan tar upp. Syftet är att göra läsaren nyfiken på att lyssna, utan att tonen blir säljig eller överdriven.
   * 2-3 meningar som sammanfattar helheten.
   * En punktlista med de viktigaste lärdomarna, bibelställena eller diskussionsämnena.
4. Välj dessutom ut 1 till 3 lämpliga tags för predikan från följande lista (exakt som de står här, hitta inte på egna taggar och skriv inte om dem):
   [Tro & Tvivel, Relationer & Familj, Bibeln & Teologi, Livskris & Hopp, Vardagskristendom, Lärjungaskap & Efterföljelse, Församling & Gemenskap, Högtider & Kyrkoåret, Guds karaktär]
5. Skapa en title med talarens namn och en kort rubrik till predikan   

SÄRSKILT UNDANTAG: Om transkriptet är för rörigt, ofullständigt eller osammanhängande för att kunna sammanfattas på ett tillförlitligt sätt:
- Sätt "description" till EXAKT texten: "Texten kunde inte sammanfattas på ett tillförlitligt sätt."
- Sätt "tags" till en tom lista: []
- Sätt "title" till ENBART talarens namn, inget annat.

Regler:
- Behåll en professionell men lättläst ton.
- Hitta inte på fakta eller bibelord som inte nämns i texten.

Svara ENDAST med ett giltigt JSON-objekt (ingen extra text, inga markdown-taggar,
inga inledande eller avslutande kommentarer) med exakt dessa nycklar:
{
  "title": "...",
  "description": "...",
  "tags": ["...", "..."]
}
"""


def enrich_metadata(
    transcript: str,
    speaker: str,
    need_title: bool,
    need_description: bool,
) -> dict:
    """
    Genererar titel/beskrivning/taggar utifrån transkriptet, med den AI-leverantör
    som är konfigurerad i .env (config.AI_PROVIDER).
    """
    truncated_transcript = transcript[:12000]
    user_prompt = f"""Talare: {speaker}

Transkript av predikan:
\"\"\"
{truncated_transcript}
\"\"\"

Generera titel, beskrivning och taggar enligt instruktionerna."""

    if config.AI_PROVIDER == "ollama":
        data = _enrich_ollama(user_prompt)
    else:
        data = _enrich_openai(user_prompt)

    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    tags = data.get("tags", [])

    # Skyddsnät: om AI:n flaggat texten som osammanfattningsbar ska inga
    # taggar följa med, oavsett vad modellen råkade returnera i "tags".
    if "kunde inte sammanfattas" in description.lower():
        tags = []

    return {"title": title, "description": description, "tags": tags}


def _enrich_openai(user_prompt: str) -> dict:
    from openai import OpenAI

    if not config.OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY saknas i .env - krävs för AI-berikning med OpenAI. "
            "Sätt AI_PROVIDER=ollama i .env om du vill köra helt lokalt istället."
        )

    client = OpenAI(api_key=config.OPENAI_API_KEY)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        response_format={"type": "json_object"},
    )

    return json.loads(response.choices[0].message.content)


def _enrich_ollama(user_prompt: str) -> dict:
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
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.7},
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

    content = response.json().get("message", {}).get("content", "")
    return _parse_json_loose(content)


def _parse_json_loose(content: str) -> dict:
    """
    Lokala modeller lyder inte alltid JSON-formatet perfekt (kan t.ex. lägga
    till ```json-block runt svaret). Detta försöker parsa ändå.
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise RuntimeError(
            f"Kunde inte tolka AI-svaret som JSON. Rått svar: {content[:300]}"
        )
