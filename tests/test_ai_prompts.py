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
    prompts = []
    monkeypatch.setattr(ai_enrichment, "_call_ai", lambda prompt, **kw: prompts.append(prompt) or "Svar")
    monkeypatch.setattr(config, "AI_TITLE_PROMPT", "   ")
    ai_enrichment.generate_title("TRANSKRIPT", "Anna")
    assert prompts[-1] == ai_enrichment.TITLE_PROMPT_TEMPLATE.format(speaker="Anna", transcript="TRANSKRIPT")


@pytest.fixture
def captured_env(monkeypatch):
    written = {}
    monkeypatch.setattr(setup.env_file, "set_values", lambda updates: written.update(updates))
    monkeypatch.setattr(setup.config, "reload", lambda: None)
    return written


def test_setup_returns_effective_and_default_prompts(client, monkeypatch):
    monkeypatch.setattr(config, "AI_TITLE_PROMPT", "")
    monkeypatch.setattr(config, "AI_DESCRIPTION_PROMPT", "Egen {transcript}")
    data = client.get("/api/setup/config").json()
    assert data["ai_title_prompt"] == ai_enrichment.TITLE_PROMPT_TEMPLATE
    assert data["ai_title_prompt_custom"] is False
    assert data["ai_description_prompt"] == "Egen {transcript}"
    assert data["ai_description_prompt_custom"] is True
    assert data["ai_description_prompt_default"] == ai_enrichment.DESCRIPTION_PROMPT_TEMPLATE


def test_setup_saves_default_prompt_as_empty(client, captured_env):
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
    res = client.post("/api/setup/save", json={"values": {"AI_TITLE_PROMPT": "Skriv en titel", "LOG_LEVEL": "INFO"}})
    assert res.status_code == 400
    assert "{transcript}" in res.json()["detail"]
    assert captured_env == {}  # inget sparat
