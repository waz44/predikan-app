"""
Tester för P1-säkerhets-/robusthetsfixarna:
- OpenAI Whisper API:s 25 MB-gräns ger ett begripligt fel (modules/transcription.py)
- Bekräftelsemailets HTML escapar interpolerade värden (modules/email_notifier.py)
"""
import pytest

import config
from modules import email_notifier, transcription


def test_openai_whisper_rejects_too_large_file(tmp_path, monkeypatch):
    """
    OpenAI tar emot högst 25 MB. En för stor fil ska ge ett begripligt fel
    redan innan något skickas - gränsen sänks här i stället för att skapa en
    riktig 25 MB-fil.
    """
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    # Sänk gränsen så en liten testfil räknas som "för stor" - vi vill testa
    # grinden, inte skriva en 25 MB-fil till disk.
    monkeypatch.setattr(transcription, "_OPENAI_WHISPER_MAX_BYTES", 4)

    audio = tmp_path / "sermon.mp3"
    audio.write_bytes(b"12345")  # 5 byte > 4

    with pytest.raises(RuntimeError, match="25 MB"):
        transcription._transcribe_openai(audio)


def test_email_html_escapes_interpolated_values(monkeypatch):
    """
    Titel, talare och taggar kan komma från AI:n och innehålla < > &. I
    mailets HTML-version ska de visas som text, inte tolkas som HTML.
    SMTP-servern byts mot en låtsasserver som bara fångar meddelandet.
    """
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USER", "from@example.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(config, "NOTIFY_EMAIL", "to@example.com")

    captured = {}

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            captured["msg"] = msg

    monkeypatch.setattr(email_notifier.smtplib, "SMTP", _FakeSMTP)

    sent = email_notifier.send_publish_confirmation(
        title="<b>Tro</b> & Tvivel",
        speaker="A & B",
        description="rad1\nrad2",
        episode_url="https://example.com/e/1",
        tags=["Tro & Tvivel"],
        processing_seconds=12.0,
    )
    assert sent is True

    html_part = next(
        part.get_payload(decode=True).decode("utf-8")
        for part in captured["msg"].walk()
        if part.get_content_type() == "text/html"
    )
    # De råa tecknen ska vara escapade, inte finnas kvar som HTML.
    assert "<b>Tro</b>" not in html_part
    assert "&lt;b&gt;Tro&lt;/b&gt; &amp; Tvivel" in html_part
    assert "A &amp; B" in html_part
