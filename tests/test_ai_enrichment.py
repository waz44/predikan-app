"""
Tester för modules/ai_enrichment.py:generate_description - specifikt
skyddsnätet mot att AI-modellen (särskilt lokala Ollama-modeller) råkar
svara med en omskrivning av SJÄLVA PROMPTEN istället för att följa den.
Bekräftat i praktiken på tre redan publicerade avsnitt, se
_looks_like_prompt_leak/_PROMPT_LEAK_MARKERS.
"""
import config
from modules import ai_enrichment

LEAKED_RESPONSE = (
    "En inledning på 2-3 meningar som lyfter fram en central fråga, ett "
    "dilemma eller ett mänskligt behov som predikan tar upp."
)
MISSING_INTRO_RESPONSE = "Viktiga punkter:\n- En sak\n- En annan sak\n\nSammanfattning:\nKort sammanfattning."
GOOD_RESPONSE = "Ibland känns tron avlägsen. Denna predikan handlar om hopp.\n\nTalare: Test"


def test_looks_like_prompt_leak_detects_known_markers():
    """
    Ett svar som återger promptens instruktioner (även i versaler) känns
    igen som läckt, medan en vanlig beskrivning godkänns.
    """
    assert ai_enrichment._looks_like_prompt_leak(LEAKED_RESPONSE) is True
    assert ai_enrichment._looks_like_prompt_leak(LEAKED_RESPONSE.upper()) is True
    assert ai_enrichment._looks_like_prompt_leak(GOOD_RESPONSE) is False


def test_looks_like_prompt_leak_detects_missing_intro():
    """Den vanligare varianten (6 av 39 riktiga avsnitt): modellen hoppar över steg 1 helt."""
    assert ai_enrichment._looks_like_prompt_leak(MISSING_INTRO_RESPONSE) is True
    assert ai_enrichment._looks_like_prompt_leak("  Viktiga punkter:\n- X") is True


def test_generate_description_returns_first_response_when_clean(tmp_env, monkeypatch):
    """
    Ett bra svar används direkt - inget onödigt nytt anrop till AI:n.
    """
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    calls = []

    def fake_openai(prompt, temperature=None):
        calls.append(prompt)
        return GOOD_RESPONSE

    monkeypatch.setattr(ai_enrichment, "_call_openai", fake_openai)

    result = ai_enrichment.generate_description("Transkript.", "Anna")
    assert result == GOOD_RESPONSE
    assert len(calls) == 1, "ska inte försöka igen när första svaret redan är bra"


def test_generate_description_retries_once_after_leak_then_succeeds(tmp_env, monkeypatch):
    """
    Ett läckt första svar ger exakt ett nytt försök, och det andra (bra)
    svaret används.
    """
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    responses = [LEAKED_RESPONSE, GOOD_RESPONSE]

    def fake_openai(prompt, temperature=None):
        return responses.pop(0)

    monkeypatch.setattr(ai_enrichment, "_call_openai", fake_openai)

    result = ai_enrichment.generate_description("Transkript.", "Anna")
    assert result == GOOD_RESPONSE
    assert responses == [], "båda fördefinierade svaren ska ha konsumerats (ett omförsök gjordes)"


def test_generate_description_falls_back_after_two_leaks(tmp_env, monkeypatch):
    """
    Läcker båda försöken används reservtexten - hellre det än en trasig
    beskrivning. Högst två anrop görs.
    """
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    calls = []

    def fake_openai(prompt, temperature=None):
        calls.append(prompt)
        return LEAKED_RESPONSE

    monkeypatch.setattr(ai_enrichment, "_call_openai", fake_openai)

    result = ai_enrichment.generate_description("Transkript.", "Anna")
    assert result == ai_enrichment.QUALITY_FALLBACK_TEXT
    assert len(calls) == 2, "ska försöka exakt en gång till innan den ger upp"
