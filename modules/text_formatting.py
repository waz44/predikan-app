"""
Modul: text_formatting
Konverterar VANLIG TEXT (radbrytningar som bokstavliga \n) - det format
beskrivningar lagras/redigeras som internt, se modules/ai_enrichment.py
och routers/spreaker_episodes.py - till HTML med <br>-radbrytningar, för
de fåtal ställen där en beskrivning faktiskt renderas som HTML av OSS:
just nu bara bekräftelsemailets HTML-del (modules/email_notifier.py).

VIKTIGT - används INTE mot Spreakers API: Spreakers "description"-fält är
dokumenterat och verifierat (mot ett riktigt konto) som RENT TEXT-fält -
HTML-taggar man skickar in stryks tyst bort där. Spreaker genererar redan
själva ett skrivskyddat "description_html"-fält (radbrytningar -> <br />)
utifrån den vanliga texten, så konsumenter (t.ex. en webbplats som visar
avsnittslistan) som vill ha HTML-formaterad text ska läsa DET fältet
istället för "description" - se modules/spreaker_client.py:s kommentarer.

to_html() är IDEMPOTENT (normaliserar bort ev. redan existerande
<br>-taggar/HTML-escapade tecken innan den escapar/infogar på nytt) - det
går alltså bra att köra samma text genom den flera gånger utan att
radbrytningarna staplas eller escapas dubbelt.
"""
import html
import re

_BR_TAG = re.compile(r"<br\s*/?>\n?", re.IGNORECASE)


def to_html(text: str) -> str:
    """Vanlig text -> HTML som bevarar radbrytningar (<br>) när den renderas som HTML."""
    plain = from_html(text)
    escaped = html.escape(plain)
    return escaped.replace("\n", "<br>\n")


def from_html(text: str) -> str:
    """Motsatsen till to_html() - normaliserar HTML-formaterad text (om någon) tillbaka till ren text."""
    without_br = _BR_TAG.sub("\n", text)
    return html.unescape(without_br)
