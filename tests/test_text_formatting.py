"""Enhetstester för modules/text_formatting.py - se den modulens docstring för bakgrunden till varför den finns."""
from modules import text_formatting


def test_to_html_converts_newlines_to_br():
    assert text_formatting.to_html("Rad 1\nRad 2") == "Rad 1<br>\nRad 2"


def test_to_html_escapes_special_characters():
    assert text_formatting.to_html("Tro & <tvivel>") == "Tro &amp; &lt;tvivel&gt;"


def test_to_html_is_idempotent():
    original = "Stycke 1\n\nStycke 2\nTalare: Anna & Bertil"
    once = text_formatting.to_html(original)
    twice = text_formatting.to_html(once)
    assert once == twice


def test_from_html_reverses_to_html():
    original = "Stycke 1\n\nStycke 2\nTalare: Anna"
    assert text_formatting.from_html(text_formatting.to_html(original)) == original


def test_from_html_handles_plain_text_without_br():
    # Text som ALDRIG gått igenom to_html() (t.ex. äldre avsnitt publicerade
    # innan detta infördes) ska komma tillbaka oförändrad.
    assert text_formatting.from_html("Bara vanlig text\nutan br-taggar") == "Bara vanlig text\nutan br-taggar"
