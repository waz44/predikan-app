"""
Egna AI-prompter för titel/beskrivning (AI_TITLE_PROMPT / AI_DESCRIPTION_PROMPT):
- flerradiga prompter överlever en rundtur genom .env (env_file + python-dotenv)
- ai_enrichment använder den egna prompten och fyller bara i kända platshållare
- inställningsguiden validerar och sparar standardprompten som tom
"""
import pytest
from dotenv import dotenv_values

import config
from modules import ai_enrichment, env_file
from routers import setup


def test_multiline_prompt_roundtrips_through_env_file(tmp_path, monkeypatch):
    """
    En prompt med radbrytningar, citattecken och bakstreck sparas på EN rad i
    .env, läses tillbaka exakt av python-dotenv och överlever att en annan
    nyckel sparas senare.
    """
    monkeypatch.setattr(env_file, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(env_file, "_EXAMPLE_PATH", tmp_path / ".env-example")
    prompt = 'Rad 1 med "citat" och \\backslash\\\r\nRad 2 {transcript}\n\n  - punkt'
    env_file.set_values({"AI_TITLE_PROMPT": prompt, "OLLAMA_MODEL": "llama3.1"})

    lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    # Prompten ryms på EN rad - inga lösa fortsättningsrader i filen.
    assert sum(1 for line in lines if line.startswith("AI_TITLE_PROMPT=")) == 1
    assert not any(line.startswith("Rad 2") for line in lines)
    values = dotenv_values(tmp_path / ".env")
    assert values["AI_TITLE_PROMPT"] == prompt.replace("\r\n", "\n")
    assert values["OLLAMA_MODEL"] == "llama3.1"

    # En senare uppdatering av en annan nyckel lämnar prompten intakt.
    env_file.set_values({"OLLAMA_MODEL": "qwen"})
    assert dotenv_values(tmp_path / ".env")["AI_TITLE_PROMPT"] == prompt.replace("\r\n", "\n")


def test_custom_prompts_are_used_with_placeholders(monkeypatch):
    """
    Egna prompter används, och bara de kända platshållarna fylls i - andra
    klammerparenteser (här ett JSON-exempel och {okand}) lämnas orörda.
    """
    prompts = []
    monkeypatch.setattr(ai_enrichment, "_call_ai", lambda prompt, **kw: prompts.append(prompt) or "Svar")
    monkeypatch.setattr(config, "AI_TITLE_PROMPT", 'Titel för {speaker}. JSON: {"a": 1} {okand}\n{transcript}')
    monkeypatch.setattr(config, "AI_DESCRIPTION_PROMPT", "Beskriv {transcript} av {speaker}, annars {fallback_text}")

    assert ai_enrichment.generate_title("TRANSKRIPT", "Anna") == "Svar"
    assert prompts[-1] == 'Titel för Anna. JSON: {"a": 1} {okand}\nTRANSKRIPT'

    ai_enrichment.generate_description("TRANSKRIPT", "Anna")
    assert prompts[-1] == f"Beskriv TRANSKRIPT av Anna, annars {ai_enrichment.QUALITY_FALLBACK_TEXT}"


def test_custom_description_prompt_skips_default_leak_check(monkeypatch):
    """Läckkontrollen gäller standardpromptens struktur - inte en egen prompt."""
    monkeypatch.setattr(ai_enrichment, "_call_ai", lambda prompt, **kw: "Viktiga punkter:\n- en")
    monkeypatch.setattr(config, "AI_DESCRIPTION_PROMPT", "Lista punkter ur {transcript}")
    assert ai_enrichment.generate_description("t", "Anna") == "Viktiga punkter:\n- en"

    monkeypatch.setattr(config, "AI_DESCRIPTION_PROMPT", "")
    assert ai_enrichment.generate_description("t", "Anna") == ai_enrichment.QUALITY_FALLBACK_TEXT


def test_empty_setting_uses_default_prompt(monkeypatch):
    """
    En prompt med bara blanksteg räknas som tom, så standardprompten används.
    """
    prompts = []
    monkeypatch.setattr(ai_enrichment, "_call_ai", lambda prompt, **kw: prompts.append(prompt) or "Svar")
    monkeypatch.setattr(config, "AI_TITLE_PROMPT", "   ")
    ai_enrichment.generate_title("TRANSKRIPT", "Anna")
    assert prompts[-1] == ai_enrichment.TITLE_PROMPT_TEMPLATE.format(speaker="Anna", transcript="TRANSKRIPT")


@pytest.fixture
def captured_env(monkeypatch):
    """
    Fångar det som skulle ha skrivits till .env, utan att röra någon fil.

    Returns:
        En dict som fylls med de värden som sparas.
    """
    written = {}
    monkeypatch.setattr(setup.env_file, "set_values", lambda updates: written.update(updates))
    monkeypatch.setattr(setup.config, "reload", lambda: None)
    return written


def test_setup_returns_effective_and_default_prompts(client, monkeypatch):
    """
    Inställningssidan får den prompt som gäller (egen eller standard), själva
    standardprompten och om prompten är egen.
    """
    monkeypatch.setattr(config, "AI_TITLE_PROMPT", "")
    monkeypatch.setattr(config, "AI_DESCRIPTION_PROMPT", "Egen {transcript}")
    data = client.get("/api/setup/config").json()
    assert data["ai_title_prompt"] == ai_enrichment.TITLE_PROMPT_TEMPLATE
    assert data["ai_title_prompt_custom"] is False
    assert data["ai_description_prompt"] == "Egen {transcript}"
    assert data["ai_description_prompt_custom"] is True
    assert data["ai_description_prompt_default"] == ai_enrichment.DESCRIPTION_PROMPT_TEMPLATE


def test_setup_saves_default_prompt_as_empty(client, captured_env):
    """
    En prompt som är identisk med standarden (även med Windows-radslut och
    en extra radbrytning) sparas som tom; en egen prompt sparas med vanliga
    radbrytningar.
    """
    res = client.post(
        "/api/setup/save",
        json={"values": {
            "AI_TITLE_PROMPT": ai_enrichment.TITLE_PROMPT_TEMPLATE.replace("\n", "\r\n") + "\n",
            "AI_DESCRIPTION_PROMPT": "Egen prompt\r\nmed {transcript}",
        }},
    )
    assert res.status_code == 200
    assert captured_env["AI_TITLE_PROMPT"] == ""
    assert captured_env["AI_DESCRIPTION_PROMPT"] == "Egen prompt\nmed {transcript}"


def test_setup_rejects_prompt_without_transcript(client, captured_env):
    """
    En prompt utan {transcript} avvisas, och INGENTING sparas - inte heller
    de andra värdena i samma anrop.
    """
    res = client.post("/api/setup/save", json={"values": {"AI_TITLE_PROMPT": "Skriv en titel", "LOG_LEVEL": "INFO"}})
    assert res.status_code == 400
    assert "{transcript}" in res.json()["detail"]
    assert captured_env == {}  # inget sparat


# ---------------------------------------------------------------- temperatur, kontext, korta ner, titelstädning

def test_temperature_setting_is_used_and_retry_goes_higher(monkeypatch):
    """
    Den inställda temperaturen används i första anropet, och omförsöket
    efter ett läckt svar körs med högre temperatur.
    """
    temps = []
    responses = ["Viktiga punkter:\n- saknar inledning", "Bra inledning.\n\nViktiga punkter:\n- x"]
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    monkeypatch.setattr(config, "AI_DESCRIPTION_PROMPT", "")
    monkeypatch.setattr(config, "AI_TEMPERATURE", 0.1)
    monkeypatch.setattr(ai_enrichment, "_save_debug", lambda *a, **kw: None)
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt, temperature: temps.append(temperature) or responses.pop(0))

    ai_enrichment.generate_description("t", "Anna")
    assert temps == [0.1, ai_enrichment.RETRY_TEMPERATURE]


def test_ollama_request_sets_temperature_and_context_window(monkeypatch):
    """
    Anropet till Ollama skickar både temperatur och kontextfönster (num_ctx).
    """
    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"message": {"content": "Svar"}}

    def fake_post(url, json, timeout):
        sent.update(json)
        return _Resp()

    monkeypatch.setattr(ai_enrichment.requests, "post", fake_post)
    monkeypatch.setattr(config, "OLLAMA_NUM_CTX", 16384)
    assert ai_enrichment._call_ollama("prompt", 0.2) == "Svar"
    assert sent["options"]["temperature"] == 0.2
    assert sent["options"]["num_ctx"] == 16384


def test_long_transcript_is_trimmed_in_the_middle():
    """
    Ett för långt transkript kortas i mitten: början och slutet finns kvar,
    med "[...]" emellan. Ett kort transkript ändras inte.
    """
    limit = ai_enrichment.TRANSCRIPT_CHAR_LIMIT
    transcript = "B" * 1000 + "M" * (limit * 2) + "S" * 1000
    trimmed = ai_enrichment._trim_transcript(transcript)
    assert len(trimmed) <= limit + 20
    assert trimmed.startswith("B" * 1000)
    assert trimmed.endswith("S" * 1000)  # slutet av predikan finns kvar
    assert "[...]" in trimmed
    assert ai_enrichment._trim_transcript("kort") == "kort"


def test_clean_title():
    """
    Titelstädningen: första raden, utan "Titel:", citattecken och punkt.
    Med standardprompten läggs talaren till om den saknas; med en egen
    prompt lämnas formen orörd.
    """
    clean = ai_enrichment._clean_title
    assert clean('Titel: "Anna: Nåd som räcker."\nFörklaring...', "Anna", True) == "Anna: Nåd som räcker"
    assert clean("Nåd som räcker", "Anna", True) == "Anna: Nåd som räcker"
    assert clean("Nåd som räcker", "Anna", False) == "Nåd som räcker"  # egen prompt: rör inte formen
    assert clean("  \n ", "Anna", True) == "Anna"


def test_tags_accept_one_per_line(monkeypatch):
    """
    Taggar tolkas även när modellen svarar med en tagg per rad och
    streck framför, och påhittade taggar sorteras bort.
    """
    monkeypatch.setattr(ai_enrichment, "_call_ai", lambda prompt, **kw: "- Tro & Tvivel\n- Guds karaktär\n- Påhittad")
    assert ai_enrichment.generate_tags("t") == ["Tro & Tvivel", "Guds karaktär"]


def test_invalid_numeric_settings_fall_back_to_defaults(monkeypatch):
    """
    Ogiltiga eller tomma talinställningar ger standardvärdet i stället för ett fel.
    """
    monkeypatch.setenv("AI_TEMPERATURE", "inte-ett-tal")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "")
    assert config._float_env("AI_TEMPERATURE", 0.2) == 0.2
    assert config._int_env("OLLAMA_NUM_CTX", 16384) == 16384


def test_setup_validates_temperature_and_context(client, captured_env):
    """
    Temperatur och kontextfönster kontrolleras när de sparas: decimalkomma
    godtas, värden utanför gränserna avvisas och tomma fält betyder
    standardvärdet.
    """
    ok = client.post("/api/setup/save", json={"values": {"AI_TEMPERATURE": "0,3", "OLLAMA_NUM_CTX": "16384"}})
    assert ok.status_code == 200
    assert captured_env["AI_TEMPERATURE"] == "0.3"
    assert captured_env["OLLAMA_NUM_CTX"] == "16384"

    captured_env.clear()
    assert client.post("/api/setup/save", json={"values": {"AI_TEMPERATURE": "5"}}).status_code == 400
    assert client.post("/api/setup/save", json={"values": {"AI_TEMPERATURE": "varm"}}).status_code == 400
    assert client.post("/api/setup/save", json={"values": {"OLLAMA_NUM_CTX": "512"}}).status_code == 400
    assert captured_env == {}

    # Tomt = standardvärdet.
    assert client.post("/api/setup/save", json={"values": {"AI_TEMPERATURE": "", "OLLAMA_NUM_CTX": ""}}).status_code == 200


def test_default_prompts_share_prefix_for_ollama_prompt_cache(monkeypatch):
    """
    Titel-, beskrivnings- och taggprompten måste börja med EXAKT samma text
    till och med transkriptet - då kan Ollama återanvända det den redan läst
    och bara första anropet behöver läsa hela predikan.
    """
    prompts = []
    monkeypatch.setattr(ai_enrichment, "_call_ai", lambda prompt, **kw: prompts.append(prompt) or "Svar")
    monkeypatch.setattr(config, "AI_TITLE_PROMPT", "")
    monkeypatch.setattr(config, "AI_DESCRIPTION_PROMPT", "")
    transcript = "Detta är predikan. " * 50

    ai_enrichment.generate_title(transcript, "Anna")
    ai_enrichment.generate_description(transcript, "Anna")
    ai_enrichment.generate_tags(transcript, "Anna")

    shared = ai_enrichment._fill(ai_enrichment._SHARED_PREFIX, speaker="Anna", transcript=transcript)
    assert all(p.startswith(shared) for p in prompts)
    assert len({p[len(shared):] for p in prompts}) == 3  # bara uppgiften skiljer


def test_description_template_leftovers_are_removed(monkeypatch):
    """Riktigt llama3.1-svar: mallens platshållare skrevs ut som en egen rad först."""
    raw = (
        "[Inledning]\nThomas Holst pratar om vem vi väljer att tillbe.\n\n"
        "Viktiga punkter:\n- **Gud** ville ha frivilliga tillbedjare.\n\n"
        "Sammanfattning:\nEn uppmaning att välja rätt."
    )
    monkeypatch.setattr(config, "AI_DESCRIPTION_PROMPT", "")
    monkeypatch.setattr(ai_enrichment, "_call_ai", lambda prompt, **kw: raw)
    result = ai_enrichment.generate_description("t", "Thomas Holst")
    assert result.startswith("Thomas Holst pratar om")
    assert "[Inledning]" not in result and "**" not in result
    assert "- Gud ville ha frivilliga tillbedjare." in result

    # Med en egen prompt lämnas svaret orört.
    monkeypatch.setattr(config, "AI_DESCRIPTION_PROMPT", "Egen {transcript}")
    assert ai_enrichment.generate_description("t", "Thomas Holst") == raw
