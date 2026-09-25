"""
Skyddsnät mot att körberoendena i requirements.txt och pyproject.toml
([project.dependencies]) glider isär. Båda finns kvar för olika arbetsflöden
(pip install -r vs pip install .), så denna test fångar drift automatiskt i
CI i stället för att lita på att de hålls i synk för hand.

De lokala Whisper-paketen (openai-whisper/faster-whisper) räknas bort - de
ligger MEDVETET som valfria extras i pyproject men listas i requirements.txt
eftersom USE_LOCAL_WHISPER=true är förvalt (se filernas kommentarer).
"""
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WHISPER_PACKAGES = {"openai-whisper", "faster-whisper"}


def _norm(req: str) -> str:
    """Normaliserar en kravsträng för jämförelse (utan blanksteg/citattecken, gemener)."""
    return req.replace(" ", "").replace('"', "").replace("'", "").lower()


def _pkg_name(req: str) -> str:
    """
    Paketets namn utan version och tillval: "requests==2.32.3" -> "requests".

    Args:
        req: En kravrad, t.ex. "uvicorn[standard]==0.30.6".

    Returns:
        Namnet i gemener.
    """
    name = req.strip().lower()
    for sep in ("==", ">=", "<=", "~=", ">", "<", ";", "["):
        name = name.split(sep)[0]
    return name.strip()


def _requirements_txt_runtime() -> set[str]:
    """
    Körberoendena i requirements.txt, utan kommentarer och Whisper-paket.

    Returns:
        Normaliserade kravrader.
    """
    lines = (_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    deps = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if _pkg_name(line) in _WHISPER_PACKAGES:
            continue
        deps.add(_norm(line))
    return deps


def _pyproject_runtime() -> set[str]:
    """
    Körberoendena i pyproject.toml ([project.dependencies]).

    Returns:
        Normaliserade kravrader.
    """
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {_norm(dep) for dep in data["project"]["dependencies"]}


def test_runtime_dependencies_match():
    """
    Båda filerna ska lista exakt samma körberoenden. Vid skillnad visar
    felet vilka rader som bara finns i den ena filen.
    """
    from_txt = _requirements_txt_runtime()
    from_toml = _pyproject_runtime()
    assert from_txt == from_toml, (
        "requirements.txt och pyproject.toml [project.dependencies] har glidit isär.\n"
        f"Bara i requirements.txt: {sorted(from_txt - from_toml)}\n"
        f"Bara i pyproject.toml:   {sorted(from_toml - from_txt)}"
    )
