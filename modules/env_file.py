"""
Modul: env_file
Läser och skriver .env-filen på plats, med bevarade kommentarer och radordning.
Används av inställningsguiden (routers/setup.py) för att spara det användaren
fyller i utan att förstöra den handskrivna .env-example-strukturen.

En rad uppdateras på plats om nyckeln redan finns (även om den låg
utkommenterad som `#KEY=`), annars läggs den till sist. Värden radbryts
aldrig och citeras bara vid behov (om de innehåller blanksteg eller #), så
en vanlig token/nyckel skrivs rått precis som en handredigerad .env.
"""
# re: känna igen raderna "NYCKEL=värde" (även utkommenterade).
import re

# config.BASE_DIR: appens mapp, där .env och .env-example ligger.
import config

# .env ligger alltid bredvid appen - samma fil som config.py läser in.
ENV_PATH = config.BASE_DIR / ".env"
# Mallen som kopieras till .env första gången guiden sparar något, så att
# den nya .env får alla förklarande kommentarer från början.
_EXAMPLE_PATH = config.BASE_DIR / ".env-example"

# Radmönster: valfria inledande blanksteg, ev. "#", NYCKEL, "=", resten.
# Fångar även utkommenterade nycklar (t.ex. "# SPREAKER_SHOW_ID=") så att
# guiden kan fylla i dem i stället för att lägga till en dubblett sist.
_LINE_RE = re.compile(r"^(\s*)(#\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _quote_if_needed(value: str) -> str:
    """
    Citerar värdet bara om det innehåller tecken som annars bryter parsningen.
    Radbrytningar (t.ex. i AI-prompterna) skrivs som \\n inom citattecken, så
    värdet alltid ryms på EN rad i .env - python-dotenv avkodar \\n tillbaka
    till en radbrytning i dubbelciterade värden, och resten av den här
    modulen (som läser .env rad för rad) fortsätter att fungera.
    """
    # Citattecken behövs för tomma värden och för värden med blanksteg,
    # "#" (som annars tolkas som början på en kommentar) eller citattecken.
    if value == "" or re.search(r"[\s#\"']", value):
        # Ordningen är viktig: bakstreck dubbleras FÖRST, annars skulle
        # bakstrecken som läggs till i stegen efter också dubbleras.
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            # Windows-radslut (CRLF) och gamla Mac-radslut (CR) görs om till
            # vanliga radbrytningar, som sedan skrivs som \n.
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\\n")
        )
        return '"' + escaped + '"'
    # Enkla värden (en token, ett tal, true/false) skrivs som de är.
    return value


def read_values() -> dict[str, str]:
    """
    Läser aktiva (icke-utkommenterade) nyckel/värde-par ur .env. Saknas filen returneras {}.

    Returns:
        En dict med nyckel -> värde, med eventuella citattecken borttagna.
        Utkommenterade rader ("# NYCKEL=...") räknas inte.
    """
    values: dict[str, str] = {}
    # Ingen .env ännu (t.ex. första starten) - då finns inga värden.
    if not ENV_PATH.exists():
        return values
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        # Grupperna i _LINE_RE: 1 = indrag, 2 = ev. "#", 3 = nyckel, 4 = värde.
        m = _LINE_RE.match(raw)
        if m and not m.group(2):  # ingen "#" -> aktiv rad
            values[m.group(3)] = _strip_quotes(m.group(4).strip())
    return values


def _strip_quotes(value: str) -> str:
    """
    Tar bort ett omslutande par citattecken ('...' eller "...") från ett värde.

    Args:
        value: Värdet som det står efter likhetstecknet i .env.

    Returns:
        Värdet utan omslutande citattecken. Escape-sekvenser (bakstreck följt
        av n eller citattecken) avkodas inte här - det gör python-dotenv när
        appen läser inställningarna.
    """
    value = value.strip()
    # Bara ett MATCHANDE par tas bort - "abc" och 'abc', men inte "abc'.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def set_values(updates: dict[str, str]) -> None:
    """
    Skriver in/uppdaterar nyckel/värde-par i .env.

    - Finns nyckeln redan (aktiv ELLER utkommenterad) uppdateras och
      av-kommenteras den raden på plats, med bevarad indentering.
    - Saknas den läggs den till sist under en genererad rubrik.
    - Filen skapas från .env-example (om den finns) första gången, annars tom.

    Övriga rader (kommentarer, blankrader, orörda nycklar) lämnas exakt som de var.
    """
    # Inget att spara - rör inte filen alls.
    if not updates:
        return

    # Första gången: utgå från mallen, så den nya .env får alla förklaringar.
    if not ENV_PATH.exists():
        if _EXAMPLE_PATH.exists():
            ENV_PATH.write_text(_EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            ENV_PATH.write_text("", encoding="utf-8")

    # Nycklar som ännu inte skrivits. Varje nyckel som hittas i filen tas
    # bort härifrån - det som blir kvar på slutet fanns inte i filen.
    remaining = dict(updates)
    out_lines: list[str] = []
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        m = _LINE_RE.match(raw)
        if m and m.group(3) in remaining:
            # Raden gäller en nyckel som ska ändras: skriv en ny rad med samma
            # indrag men UTAN "#", så en utkommenterad nyckel blir aktiv.
            key = m.group(3)
            indent = m.group(1)
            # pop() gör att en nyckel som förekommer två gånger bara skrivs om
            # första gången - den andra lämnas som den var.
            out_lines.append(f"{indent}{key}={_quote_if_needed(remaining.pop(key))}")
        else:
            # Alla andra rader (kommentarer, tomma rader, andra nycklar) orörda.
            out_lines.append(raw)

    # Nycklar som inte fanns i filen läggs sist, under en tydlig rubrik.
    if remaining:
        out_lines.append("")
        out_lines.append("# --- Tillagt av inställningsguiden ---")
        for key, value in remaining.items():
            out_lines.append(f"{key}={_quote_if_needed(value)}")

    # Skriv hela filen på en gång, med en avslutande radbrytning.
    ENV_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
