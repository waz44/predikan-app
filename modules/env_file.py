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
import re

import config

ENV_PATH = config.BASE_DIR / ".env"
_EXAMPLE_PATH = config.BASE_DIR / ".env-example"

# Radmönster: valfria inledande blanksteg, ev. "#", NYCKEL, "=", resten.
# Fångar även utkommenterade nycklar (t.ex. "# SPREAKER_SHOW_ID=") så att
# guiden kan fylla i dem i stället för att lägga till en dubblett sist.
_LINE_RE = re.compile(r"^(\s*)(#\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _quote_if_needed(value: str) -> str:
    """Citerar värdet bara om det innehåller tecken som annars bryter parsningen."""
    if value == "" or re.search(r"[\s#\"']", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def read_values() -> dict[str, str]:
    """Läser aktiva (icke-utkommenterade) nyckel/värde-par ur .env. Saknas filen returneras {}."""
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        m = _LINE_RE.match(raw)
        if m and not m.group(2):  # ingen "#" -> aktiv rad
            values[m.group(3)] = _strip_quotes(m.group(4).strip())
    return values


def _strip_quotes(value: str) -> str:
    value = value.strip()
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
    if not updates:
        return

    if not ENV_PATH.exists():
        if _EXAMPLE_PATH.exists():
            ENV_PATH.write_text(_EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            ENV_PATH.write_text("", encoding="utf-8")

    remaining = dict(updates)
    out_lines: list[str] = []
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        m = _LINE_RE.match(raw)
        if m and m.group(3) in remaining:
            key = m.group(3)
            indent = m.group(1)
            out_lines.append(f"{indent}{key}={_quote_if_needed(remaining.pop(key))}")
        else:
            out_lines.append(raw)

    if remaining:
        out_lines.append("")
        out_lines.append("# --- Tillagt av inställningsguiden ---")
        for key, value in remaining.items():
            out_lines.append(f"{key}={_quote_if_needed(value)}")

    ENV_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
