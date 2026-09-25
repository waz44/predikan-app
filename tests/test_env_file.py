"""
Tester för modules/env_file.py: att .env skrivs/uppdateras på plats med
bevarade kommentarer, att utkommenterade nycklar fylls i i stället för att
dubbleras, och att read_values bara läser aktiva rader.
"""
import pytest

from modules import env_file


@pytest.fixture
def tmp_env_file(tmp_path, monkeypatch):
    """
    Pekar om env_file mot temporära .env- och .env-example-filer.

    Returns:
        {"env": sökväg till .env, "example": sökväg till .env-example}
        (ingen av filerna finns från början).
    """
    env_path = tmp_path / ".env"
    example_path = tmp_path / ".env-example"
    monkeypatch.setattr(env_file, "ENV_PATH", env_path)
    monkeypatch.setattr(env_file, "_EXAMPLE_PATH", example_path)
    return {"env": env_path, "example": example_path}


def test_creates_from_example_when_missing(tmp_env_file):
    """
    Första sparningen skapar .env från mallen - med mallens kommentarer kvar.
    """
    tmp_env_file["example"].write_text("# rubrik\nSPREAKER_SHOW_ID=\n", encoding="utf-8")
    env_file.set_values({"SPREAKER_SHOW_ID": "12345"})
    content = tmp_env_file["env"].read_text(encoding="utf-8")
    assert "SPREAKER_SHOW_ID=12345" in content
    assert "# rubrik" in content  # kommentaren från exemplet bevarad


def test_updates_existing_key_in_place(tmp_env_file):
    """
    En befintlig nyckel uppdateras på sin plats; övriga rader och
    kommentarer lämnas orörda och ingen dubblett läggs till sist.
    """
    tmp_env_file["env"].write_text(
        "# kommentar\nAI_PROVIDER=openai\nOLLAMA_MODEL=llama3.1\n", encoding="utf-8"
    )
    env_file.set_values({"AI_PROVIDER": "ollama"})
    lines = tmp_env_file["env"].read_text(encoding="utf-8").splitlines()
    assert "# kommentar" in lines
    assert "AI_PROVIDER=ollama" in lines
    assert "OLLAMA_MODEL=llama3.1" in lines  # orörd
    # Ingen dubblett tillagd sist.
    assert sum(1 for line in lines if line.startswith("AI_PROVIDER=")) == 1


def test_uncomments_commented_key(tmp_env_file):
    """
    En utkommenterad nyckel ("# NYCKEL=") blir aktiv i stället för att läggas till igen.
    """
    tmp_env_file["env"].write_text("# SPREAKER_SHOW_ID=\n", encoding="utf-8")
    env_file.set_values({"SPREAKER_SHOW_ID": "999"})
    content = tmp_env_file["env"].read_text(encoding="utf-8")
    assert "SPREAKER_SHOW_ID=999" in content
    assert "# SPREAKER_SHOW_ID=" not in content


def test_appends_missing_key(tmp_env_file):
    """
    En nyckel som inte finns i filen läggs till sist.
    """
    tmp_env_file["env"].write_text("AI_PROVIDER=openai\n", encoding="utf-8")
    env_file.set_values({"SPREAKER_API_TOKEN": "tok"})
    content = tmp_env_file["env"].read_text(encoding="utf-8")
    assert "AI_PROVIDER=openai" in content
    assert "SPREAKER_API_TOKEN=tok" in content


def test_read_values_ignores_comments(tmp_env_file):
    """
    Bara aktiva rader läses - utkommenterade nycklar och ren text ignoreras.
    """
    tmp_env_file["env"].write_text(
        "# EMAIL_ENABLED=true\nAI_PROVIDER=ollama\n\n# bara en kommentar\n", encoding="utf-8"
    )
    values = env_file.read_values()
    assert values == {"AI_PROVIDER": "ollama"}


def test_quotes_value_with_spaces(tmp_env_file):
    """
    Värden med mellanslag skrivs inom citattecken och läses tillbaka utan dem.
    """
    tmp_env_file["env"].write_text("SMTP_USER=\n", encoding="utf-8")
    env_file.set_values({"SMTP_USER": "namn med mellanslag"})
    content = tmp_env_file["env"].read_text(encoding="utf-8")
    assert 'SMTP_USER="namn med mellanslag"' in content
    # Och att det läses tillbaka utan citattecken.
    assert env_file.read_values()["SMTP_USER"] == "namn med mellanslag"
