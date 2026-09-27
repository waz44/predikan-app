"""
Tester för modules/misheard_words.py och att ai_enrichment använder den -
bakgrund: KB-Whisper hörde "frestelse" som "frästelse" och AI:n skrev av
felstavningen i den publicerade beskrivningen.
"""
import config
from modules import ai_enrichment
from modules.misheard_words import fix_misheard_words


def test_fixes_word_and_its_inflections():
    text = "Frästelsen kom. Vi hamnar i frästelse och frästelser. Gud frästar ingen."
    assert fix_misheard_words(text) == (
        "Frestelsen kom. Vi hamnar i frestelse och frestelser. Gud frestar ingen."
    )


def test_keeps_uppercase():
    assert fix_misheard_words("FRÄSTELSE") == "FRESTELSE"


def test_leaves_real_words_alone():
    # "fräste"/"fräst" är riktiga böjningar av "fräsa", och ord utan * får
    # ingen ändelse ("frästarna" är inte samma ord som "frästar").
    text = "Katten fräste. Den är fräst. Frestelse är rätt. frästarna"
    assert fix_misheard_words(text) == text


def test_word_boundaries_include_swedish_letters():
    # Ett felhört ord mitt i ett längre ord rättas inte.
    assert fix_misheard_words("ofrästelse") == "ofrästelse"


def test_enrichment_fixes_transcript_and_answer(tmp_env, monkeypatch):
    """Både transkriptet som skickas till AI:n och svaret rättas."""
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    prompts = []

    def fake_openai(prompt, temperature=None):
        prompts.append(prompt)
        return "Vad gör vi med frästelsen?\n\nViktiga punkter:\n- Gud frästar ingen."

    monkeypatch.setattr(ai_enrichment, "_call_openai", fake_openai)

    result = ai_enrichment.generate_description("Gud frästar ingen till det onda.", "Anna")

    assert "Gud frestar ingen till det onda." in prompts[0]
    assert "fräst" not in result
    assert "frestelsen" in result and "Gud frestar ingen." in result


def test_enrichment_fixes_title(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    monkeypatch.setattr(ai_enrichment, "_call_openai", lambda prompt, temperature=None: "Anna: När prövningen blir en frästelse")

    assert ai_enrichment.generate_title("Transkript.", "Anna") == "Anna: När prövningen blir en frestelse"
