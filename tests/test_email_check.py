"""
Tester för e-posttestet (email_notifier.check_smtp_settings och
POST /api/setup/email/test) och för hur e-postanslutningen öppnas
(open_smtp). Nätverket och e-postservern byts mot låtsasversioner -
inget skickas på riktigt.
"""
import smtplib
import socket
import ssl

import pytest

import config
from modules import email_notifier


class FakeServer:
    """En inloggad, krypterad SMTP-anslutning som bara låtsas."""

    def __init__(self, login_error=None, rcpt_code=250, data_code=250):
        self.login_error = login_error
        self.rcpt_code = rcpt_code
        self.data_code = data_code
        self.sent = None
        self.sock = type("Sock", (), {"version": lambda self: "TLSv1.3"})()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        if self.login_error:
            raise self.login_error

    def mail(self, sender):
        return 250, b"OK"

    def rcpt(self, recipient):
        return self.rcpt_code, b"5.1.1 User unknown" if self.rcpt_code >= 400 else b"OK"

    def data(self, message):
        self.sent = message
        return self.data_code, b"2.0.0 OK queued as ABC123"


@pytest.fixture
def network_ok(monkeypatch):
    """Namnuppslag och anslutning lyckas; open_smtp ger en låtsasserver."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("192.0.2.10", 587))])

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _Conn())
    server = FakeServer()
    monkeypatch.setattr(email_notifier, "open_smtp", lambda host, port: server)
    return server


def _check(user="kyrkan@exempel.se", password="app-losen", recipient="ruben@exempel.se"):
    return email_notifier.check_smtp_settings("smtp.exempel.se", 587, user, password, recipient)


def _steps(result):
    return [(s["step"], s["ok"]) for s in result["steps"]]


def test_all_steps_pass_and_mail_has_spam_friendly_headers(network_ok):
    result = _check()

    assert result["ok"] is True
    assert _steps(result) == [
        ("Uppgifter", True), ("Namnuppslag", True), ("Anslutning", True),
        ("Kryptering", True), ("Inloggning", True), ("Skicka", True),
    ]
    assert "ABC123" in result["steps"][-1]["detail"]  # serverns svar syns
    assert "skräpposten" in result["steps"][-1]["hint"]
    sent = network_ok.sent.decode("utf-8")
    assert "Date:" in sent and "Message-ID:" in sent and "@exempel.se>" in result["message_id"]


def test_missing_fields_stop_at_first_step(network_ok):
    result = _check(password="")
    assert _steps(result) == [("Uppgifter", False)]
    assert "lösenord" in result["steps"][0]["detail"]


def test_username_without_at_is_a_warning(network_ok):
    result = _check(user="kyrkan")
    assert result["steps"][0]["ok"] is None
    assert result["ok"] is True


def test_unknown_host(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    result = _check()
    assert _steps(result)[-1] == ("Namnuppslag", False)
    assert "stavningen" in result["steps"][-1]["hint"]


def test_blocked_port(network_ok, monkeypatch):
    def timeout(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr(socket, "create_connection", timeout)
    result = _check()
    assert _steps(result)[-1] == ("Anslutning", False)
    assert "brandvägg" in result["steps"][-1]["hint"]


def test_tls_failure(network_ok, monkeypatch):
    def fail(host, port):
        raise ssl.SSLError("wrong version number")

    monkeypatch.setattr(email_notifier, "open_smtp", fail)
    result = _check()
    assert _steps(result)[-1] == ("Kryptering", False)
    assert "465" in result["steps"][-1]["hint"]


def test_wrong_password_explains_app_passwords(network_ok):
    network_ok.login_error = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    result = _check()
    assert _steps(result)[-1] == ("Inloggning", False)
    assert "535" in result["steps"][-1]["detail"]
    assert "app-lösenord" in result["steps"][-1]["hint"]


def test_refused_recipient(network_ok):
    network_ok.rcpt_code = 550
    result = _check()
    assert _steps(result)[-1] == ("Skicka", False)
    assert "User unknown" in result["steps"][-1]["detail"]


def test_endpoint_uses_saved_password_when_field_is_empty(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(config, "SMTP_PASSWORD", "sparat-losen")

    def fake_check(host, port, user, password, recipient):
        seen.update(host=host, port=port, password=password)
        return {"ok": True, "steps": [{"step": "Skicka", "ok": True, "detail": "ok", "hint": ""}], "message_id": "<x>"}

    monkeypatch.setattr(email_notifier, "check_smtp_settings", fake_check)
    res = client.post("/api/setup/email/test", json={
        "smtp_host": "smtp.exempel.se", "smtp_port": "465", "smtp_user": "a@b.se",
        "smtp_password": "", "notify_email": "c@d.se",
    })
    assert res.status_code == 200 and res.json()["ok"] is True
    assert seen == {"host": "smtp.exempel.se", "port": 465, "password": "sparat-losen"}

    assert client.post("/api/setup/email/test", json={"smtp_port": "abc"}).status_code == 400


def test_open_smtp_uses_ssl_on_465_and_requires_starttls_otherwise(monkeypatch):
    created = {}

    class FakeSSL:
        def __init__(self, host, port, timeout, context):
            created["ssl"] = (host, port)

    class FakePlain:
        def __init__(self, host, port, timeout):
            created["plain"] = (host, port)
            self.closed = False

        def ehlo(self):
            pass

        def has_extn(self, name):
            return False  # erbjuder ingen kryptering

        def close(self):
            created["closed"] = True

    monkeypatch.setattr(email_notifier.smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(email_notifier.smtplib, "SMTP", FakePlain)

    email_notifier.open_smtp("smtp.exempel.se", 465)
    assert created["ssl"] == ("smtp.exempel.se", 465)

    with pytest.raises(smtplib.SMTPNotSupportedError):
        email_notifier.open_smtp("smtp.exempel.se", 587)
    assert created["closed"] is True  # aldrig vidare okrypterat
