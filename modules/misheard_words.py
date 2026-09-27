"""
Modul: misheard_words
Rättar ord som taligenkänningen (Whisper/KB-Whisper) återkommande hör fel,
t.ex. "frästelse" i stället för "frestelse".

AI-modellen skriver av transkriptets stavning rakt av (bekräftat med
llama3.1: med "frästelse" i transkriptet blev det "frästelse" i beskrivningen
varje gång, med rätt stavning blev det rätt). Rättningen görs därför både på
transkriptet innan det skickas till AI:n och på AI:ns svar (se
modules/ai_enrichment.py) - det senare fångar även en egen prompt som inte
ber modellen rätta stavningen.

Lägg bara till ord som ALDRIG är rätt i en predikan. "fräste" och "fräst"
finns t.ex. inte med, eftersom de är riktiga böjningar av "fräsa".
"""
import re

# Felhört ord -> rätt stavning. Jämförs som hela ord och utan hänsyn till
# stor/liten bokstav (rättningen behåller originalets stora bokstav).
# Ett * sist betyder att ordet också rättas med valfri ändelse, t.ex.
# "frästelse*" rättar även "frästelsen", "frästelser" och "frästelserna".
MISHEARD_WORDS: dict[str, str] = {
    "frästelse*": "frestelse",
    "frästar": "frestar",
    "frästas": "frestas",
    "frästare*": "frestare",
}


def _build_pattern() -> re.Pattern:
    """
    Ett enda reguljärt uttryck för hela ordlistan, med ordstammen och
    ändelsen i varsin grupp.

    Returns:
        Uttrycket - grupp 1 är det felhörda ordet (utan ändelse), grupp 2
        ändelsen (tom för ord utan *).
    """
    alternatives = []
    # Längsta ordet först, så att t.ex. "frästare" inte skulle fångas av ett
    # kortare ord som börjar likadant.
    for word in sorted(MISHEARD_WORDS, key=len, reverse=True):
        stem = re.escape(word.rstrip("*"))
        alternatives.append(stem)
    # (?<!\w) och (?!\w) i stället för \b, så att å/ä/ö räknas som bokstäver
    # även i ordgränsen. Ändelsen fångas bara om roten har *, se _replace.
    return re.compile(rf"(?<!\w)({'|'.join(alternatives)})(\w*)(?!\w)", re.IGNORECASE)


_PATTERN = _build_pattern()
# Samma ordlista, men med gemener och utan *, för uppslagning i _replace.
_LOOKUP = {word.rstrip("*").lower(): (fixed, word.endswith("*")) for word, fixed in MISHEARD_WORDS.items()}


def _match_case(original: str, fixed: str) -> str:
    """
    Ger den rättade stavningen samma stora/små bokstäver som originalet.

    Args:
        original: Ordet som det stod i texten, t.ex. "Frästelsen".
        fixed: Rätt stavning med gemener, t.ex. "frestelsen".

    Returns:
        T.ex. "Frestelsen" (stor första bokstav) eller "FRESTELSEN".
    """
    if original.isupper() and len(original) > 1:
        return fixed.upper()
    if original[:1].isupper():
        return fixed[:1].upper() + fixed[1:]
    return fixed


def _replace(match: re.Match) -> str:
    """Rättar en träff, eller lämnar den orörd om ändelsen inte är tillåten."""
    stem, ending = match.group(1), match.group(2)
    fixed, allows_ending = _LOOKUP[stem.lower()]
    # Ord utan * rättas bara när det står ensamt ("frästar", inte "frästarna").
    if ending and not allows_ending:
        return match.group(0)
    return _match_case(stem + ending, fixed + ending.lower())


def fix_misheard_words(text: str) -> str:
    """
    Rättar alla ord i MISHEARD_WORDS i en text.

    Args:
        text: Transkript, titel eller beskrivning.

    Returns:
        Texten med de felhörda orden rättade - allt annat orört.
    """
    if not text:
        return text
    return _PATTERN.sub(_replace, text)
