"""
Modul: email_notifier
Skickar ett bekräftelsemail när ett avsnitt har publicerats, och testar
e-postinställningarna steg för steg (check_smtp_settings, knappen
"✉️ Skicka testmejl" under ⚙️ Inställningar → 📧 E-post).
Om EMAIL_ENABLED=false (standard) skickas inget mail - istället visar
frontend en sammanfattningssida med samma information.
"""
# html.escape: gör <, > och & ofarliga i HTML-versionen av mailet.
import html

# re: delar upp och kontrollerar mottagaradresserna.
import re

# smtplib: Pythons inbyggda e-postklient, som pratar direkt med en
# SMTP-server (t.ex. smtp.gmail.com). Inga extra paket behövs.
import smtplib

# socket/ssl: namnuppslag, anslutning och kryptering - testas var för sig
# av check_smtp_settings, så att det syns exakt var det tar stopp.
import socket
import ssl
from datetime import datetime

# MIMEMultipart/MIMEText bygger själva mailet: ett "kuvert" med två
# versioner av samma innehåll (ren text och HTML).
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# formatdate/make_msgid: rubrikerna Date och Message-ID. Saknas de räknar
# många spamfilter mailet som misstänkt.
from email.utils import formatdate, make_msgid

# config: SMTP-uppgifter och mottagare från .env.
import config

# text_formatting.to_html: beskrivningens radbrytningar som <br>.
from modules import text_formatting


def _format_duration(total_seconds: float | None) -> str:
    """
    Formaterar sekunder som en läsbar sträng, t.ex. '12min 4s' eller '1h 3min'.

    Används för raden "Bearbetningstid" i mailet, så mottagaren ser hur
    lång tid hela kedjan (klippning till publicering) tog.

    Args:
        total_seconds: Tid i sekunder, eller None om den inte mättes.

    Returns:
        Tiden som text, eller "-" om den saknas. Timmar visas utan sekunder,
        eftersom sekunder inte säger något när det gått över en timme.
    """
    if total_seconds is None:
        return "-"
    # Avrunda till hela sekunder; max(0, ...) skyddar mot negativa värden
    # om datorns klocka skulle ha ställts om under bearbetningen.
    seconds = max(0, int(round(total_seconds)))
    # divmod ger kvot och rest på en gång: 3725 s -> (1 h, 125 s) -> (2 min, 5 s).
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}min"
    if minutes:
        return f"{minutes}min {secs}s"
    return f"{secs}s"


def send_publish_confirmation(
    title: str,
    speaker: str,
    description: str,
    episode_url: str,
    tags: list[str] | None = None,
    processing_seconds: float | None = None,
) -> bool:
    """
    Skickar ett bekräftelsemail. Returnerar True om mailet skickades,
    False om e-post är avaktiverat i konfigurationen.

    Mailet skickas i två versioner i samma meddelande (multipart/alternative):
    en ren textversion och en HTML-version. E-postprogrammet väljer själv den
    bästa det kan visa - de flesta visar HTML, men enkla program och
    skärmläsare kan använda textversionen.

    Args:
        title: Avsnittets titel.
        speaker: Talarens namn.
        description: Beskrivningen, med radbrytningar (blir <br> i HTML).
        episode_url: Länk till avsnittet på Spreaker.
        tags: Taggarna som valdes, eller None.
        processing_seconds: Hur lång tid bearbetningen tog.

    Returns:
        True om mailet skickades, False om e-post är avstängt.

    Raises:
        RuntimeError: Om e-post är påslaget men SMTP-uppgifter saknas.
        smtplib.SMTPException: Om servern avvisar inloggning eller sändning.
            Anroparen (services/pipeline.py) loggar felet utan att fälla jobbet.
    """
    # E-post är avstängt som standard. Kön visar ändå samma sammanfattning,
    # så inget går förlorat - steget visas bara som "hoppades över".
    if not config.EMAIL_ENABLED:
        return False

    # Påslaget men ofullständigt ifyllt: ett tydligt fel är bättre än att
    # SMTP-servern svarar med något kryptiskt längre fram.
    if not all([config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD, config.NOTIFY_EMAIL]):
        raise RuntimeError(
            "EMAIL_ENABLED=true men SMTP-uppgifter saknas i .env "
            "(SMTP_HOST, SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL)."
        )

    # Värden som används i båda versionerna av mailet.
    tags_text = ", ".join(tags) if tags else "-"
    duration_text = _format_duration(processing_seconds)

    # "alternative" betyder att delarna är olika versioner av SAMMA innehåll
    # (inte bilagor) - e-postprogrammet visar bara en av dem.
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Nytt avsnitt publicerat: {title}"
    # Avsändaren är samma konto som loggar in, annars avvisar många
    # e-posttjänster (t.ex. Gmail) mailet som förfalskat.
    msg["From"] = config.SMTP_USER
    # NOTIFY_EMAIL kan innehålla flera adresser (se parse_recipients).
    recipients = parse_recipients(config.NOTIFY_EMAIL)
    msg["To"] = ", ".join(recipients)
    _add_standard_headers(msg, config.SMTP_USER)

    # Textversionen: ren text, ingen escaping behövs.
    text_body = f"""Ett nytt avsnitt har publicerats!

Titel: {title}
Talare: {speaker}
Länk: {episode_url}
Taggar: {tags_text}
Bearbetningstid: {duration_text}

Beskrivning:
{description}
"""

    # HTML-delen: escapa alla interpolerade värden så en titel/talare/tagg som
    # innehåller <, > eller & inte ger trasig HTML (eller injektion). title och
    # taggar är AI-genererade/användarinmatade, så de kan innehålla vad som
    # helst. description escapas redan av text_formatting.to_html (som dessutom
    # bevarar radbrytningar som <br>). text_body ovan är ren text och behöver
    # ingen escaping.
    esc_title = html.escape(title)
    esc_speaker = html.escape(speaker)
    esc_tags = html.escape(tags_text)
    esc_url = html.escape(episode_url, quote=True)
    html_body = f"""
    <html>
      <body style="font-family: sans-serif; color: #222;">
        <h2>🎙️ Nytt avsnitt publicerat</h2>
        <p><strong>Titel:</strong> {esc_title}</p>
        <p><strong>Talare:</strong> {esc_speaker}</p>
        <p><strong>Länk:</strong> <a href="{esc_url}">{esc_url}</a></p>
        <p><strong>Taggar:</strong> {esc_tags}</p>
        <p><strong>Bearbetningstid:</strong> {duration_text}</p>
        <p><strong>Beskrivning:</strong><br>{text_formatting.to_html(description)}</p>
      </body>
    </html>
    """

    # Ordningen spelar roll: enligt e-poststandarden ska den "bästa"
    # versionen komma SIST, så HTML läggs till efter textversionen.
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    # Anslut med kryptering, logga in och skicka. "with" stänger
    # anslutningen även om något går fel.
    with open_smtp(config.SMTP_HOST, config.SMTP_PORT) as server:
        # För Gmail krävs ett app-lösenord här, inte kontots vanliga lösenord.
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg, to_addrs=recipients)

    return True


# Sekunder att vänta på e-postservern innan försöket ges upp - utan en
# gräns kan en blockerad port få e-poststeget att hänga länge.
SMTP_TIMEOUT = 20
# Port 465 är krypterad från första början ("SMTPS"); övriga portar (oftast
# 587) börjar okrypterat och slår på krypteringen med STARTTLS.
_SSL_PORTS = {465}


# En rimlig e-postadress: något@något.något, utan blanksteg. Ingen fullständig
# kontroll - bara tillräckligt för att fånga stavfel som en saknad punkt.
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def parse_recipients(text: str) -> list[str]:
    """
    Mottagaradresserna i NOTIFY_EMAIL - en eller flera.

    Adresserna kan skiljas med kommatecken, semikolon (som i Outlook) eller
    radbrytningar. Samma adress två gånger räknas en gång.

    Args:
        text: T.ex. "pastor@exempel.se; tekniker@exempel.se".

    Returns:
        Adresserna i den ordning de står, t.ex.
        ["pastor@exempel.se", "tekniker@exempel.se"].
    """
    recipients: list[str] = []
    for part in re.split(r"[,;\n]", text or ""):
        address = part.strip()
        if address and address.lower() not in (r.lower() for r in recipients):
            recipients.append(address)
    return recipients


def invalid_recipients(recipients: list[str]) -> list[str]:
    """De adresser som inte ser ut som e-postadresser (se _EMAIL_RE)."""
    return [address for address in recipients if not _EMAIL_RE.match(address)]


def _add_standard_headers(msg, sender: str) -> None:
    """
    Lägger till rubrikerna Date och Message-ID. smtplib lägger inte till dem
    själv, och utan dem räknar många spamfilter mailet som misstänkt.

    Args:
        msg: Mailet.
        sender: Avsändarens adress - domänen används i Message-ID.
    """
    msg["Date"] = formatdate(localtime=True)
    domain = sender.split("@", 1)[1] if "@" in sender else None
    msg["Message-ID"] = make_msgid(domain=domain)


def open_smtp(host: str, port: int, timeout: float = SMTP_TIMEOUT) -> smtplib.SMTP:
    """
    Öppnar en krypterad anslutning till e-postservern.

    Port 465 krypteras direkt (SMTP_SSL); övriga portar slår på krypteringen
    med STARTTLS innan något annat skickas. Erbjuder servern ingen kryptering
    avbryts anslutningen - lösenordet ska aldrig skickas okrypterat.

    Args:
        host: E-postservern, t.ex. "smtp.gmail.com".
        port: Porten, oftast 587 (STARTTLS) eller 465 (SSL).
        timeout: Sekunder att vänta på servern.

    Returns:
        En öppen, krypterad anslutning (kan användas med "with").

    Raises:
        smtplib.SMTPNotSupportedError: Om servern inte erbjuder kryptering.
        OSError/ssl.SSLError/smtplib.SMTPException: Vid nätverks- eller TLS-fel.
    """
    context = ssl.create_default_context()
    if port in _SSL_PORTS:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    server = smtplib.SMTP(host, port, timeout=timeout)
    try:
        server.ehlo()
        if not server.has_extn("starttls"):
            raise smtplib.SMTPNotSupportedError(
                "Servern erbjuder inte kryptering (STARTTLS) på den här porten."
            )
        server.starttls(context=context)
        server.ehlo()
    except BaseException:
        server.close()
        raise
    return server


def _step(name: str, ok: bool | None, detail: str, hint: str = "") -> dict:
    """Ett steg i testresultatet. ok=None betyder en varning (testet fortsätter)."""
    return {"step": name, "ok": ok, "detail": detail, "hint": hint}


def _smtp_error_text(exc: Exception) -> str:
    """Serverns svar ("535 5.7.8 Username and Password not accepted") som läsbar text."""
    if isinstance(exc, smtplib.SMTPResponseException):
        message = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"{exc.smtp_code} {message}".strip()
    return str(exc) or exc.__class__.__name__


def check_smtp_settings(host: str, port: int, user: str, password: str, recipients_text: str) -> dict:
    """
    Testar e-postinställningarna steg för steg och skickar ett testmejl.

    Stegen körs i tur och ordning och testet stannar vid första felet, så att
    det syns var problemet sitter:
      1. Uppgifter    - allt ifyllt, och ser adresserna rimliga ut?
      2. Namnuppslag  - finns servern (DNS)?
      3. Anslutning   - når den här datorn servern på porten (brandvägg)?
      4. Kryptering   - fungerar TLS (rätt port, giltigt certifikat)?
      5. Inloggning   - godtar servern användarnamn och lösenord?
      6. Skicka       - tar servern emot mejlet till varje mottagare?
    Går alla steg igenom har mejlet lämnat appen - kommer det ändå inte fram
    ligger felet efter servern, oftast i ett spamfilter.

    Args:
        host: E-postservern.
        port: Porten.
        user: Användarnamnet (blir även avsändare).
        password: Lösenordet (för Gmail ett app-lösenord).
        recipients_text: Vart testmejlet skickas - en eller flera adresser
            (se parse_recipients), samma som NOTIFY_EMAIL.

    Returns:
        {"ok": True om mejlet togs emot, "steps": [{step, ok, detail, hint}],
         "message_id": testmejlets Message-ID (om det skickades)}.
    """
    steps: list[dict] = []
    result = {"ok": False, "steps": steps, "message_id": None}

    # 1. Uppgifter
    recipients = parse_recipients(recipients_text)
    missing = [label for label, value in (
        ("SMTP-server", host), ("port", port), ("användare", user), ("lösenord", password), ("mottagare", recipients),
    ) if not value]
    if missing:
        steps.append(_step("Uppgifter", False, "Saknas: " + ", ".join(missing) + ".", "Fyll i fälten och försök igen."))
        return result
    bad = invalid_recipients(recipients)
    if bad:
        steps.append(_step(
            "Uppgifter", False, "Ingen giltig e-postadress: " + ", ".join(bad) + ".",
            "Skilj flera adresser åt med kommatecken eller semikolon.",
        ))
        return result
    recipient_list = ", ".join(recipients)
    if "@" not in user:
        steps.append(_step(
            "Uppgifter", None, f"Användarnamnet '{user}' är ingen e-postadress, men används som avsändare.",
            "Många servrar och spamfilter avvisar mejl utan en riktig avsändaradress. "
            "Använd hela e-postadressen som användarnamn om servern tillåter det.",
        ))
    else:
        steps.append(_step("Uppgifter", True, f"Avsändare {user}, mottagare {recipient_list}."))

    # 2. Namnuppslag
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except socket.gaierror as exc:
        steps.append(_step(
            "Namnuppslag", False, f"Servern '{host}' hittades inte ({exc}).",
            "Kontrollera stavningen av SMTP-servern, och att den här datorn har internet.",
        ))
        return result
    steps.append(_step("Namnuppslag", True, f"{host} = {', '.join(addresses[:3])}"))

    # 3. Anslutning
    try:
        socket.create_connection((host, port), timeout=SMTP_TIMEOUT).close()
    except TimeoutError:
        steps.append(_step(
            "Anslutning", False, f"Ingen kontakt med {host}:{port} inom {SMTP_TIMEOUT} s.",
            "Porten blockeras troligen av en brandvägg - på datorn, i nätverket eller hos "
            "internetleverantören - eller så är porten fel. Vanligast är 587 (STARTTLS) eller "
            "465 (SSL); port 25 är ofta spärrad.",
        ))
        return result
    except OSError as exc:
        steps.append(_step(
            "Anslutning", False, f"Kunde inte ansluta till {host}:{port} ({exc}).",
            "Servern finns men tar inte emot på den porten. Prova 587 eller 465.",
        ))
        return result
    steps.append(_step("Anslutning", True, f"Den här datorn når {host} på port {port}."))

    # 4. Kryptering - och resten av testet på samma anslutning.
    try:
        server = open_smtp(host, port)
    except ssl.SSLCertVerificationError as exc:
        steps.append(_step(
            "Kryptering", False, f"Serverns certifikat godkändes inte ({exc.verify_message}).",
            "Kontrollera att SMTP-servern är exakt det namn som e-postleverantören anger.",
        ))
        return result
    except (ssl.SSLError, smtplib.SMTPException, OSError) as exc:
        steps.append(_step(
            "Kryptering", False, f"Krypteringen gick inte att starta ({_smtp_error_text(exc)}).",
            "Port 465 kräver SSL från början och 587 STARTTLS - kontrollera att porten stämmer "
            "med det e-postleverantören anger.",
        ))
        return result

    with server:
        tls_version = server.sock.version() if hasattr(server.sock, "version") else "TLS"
        steps.append(_step("Kryptering", True, f"Krypterad anslutning ({tls_version})."))

        # 5. Inloggning
        try:
            server.login(user, password)
        except smtplib.SMTPAuthenticationError as exc:
            steps.append(_step(
                "Inloggning", False, f"Servern godtog inte inloggningen: {_smtp_error_text(exc)}",
                "Fel användarnamn eller lösenord. Gmail kräver tvåstegsverifiering och ett "
                "app-lösenord (inte kontots vanliga lösenord). Microsoft 365/Outlook kräver att "
                "\"Authenticated SMTP\" är påslaget för brevlådan.",
            ))
            return result
        except (smtplib.SMTPException, OSError) as exc:
            steps.append(_step("Inloggning", False, f"Inloggningen misslyckades: {_smtp_error_text(exc)}"))
            return result
        steps.append(_step("Inloggning", True, f"Inloggad som {user}."))

        # 6. Skicka - steg för steg, så att serverns svar på varje del syns.
        msg = MIMEText(
            "Det här är ett testmejl från Predikan → Podcast.\n\n"
            "Kom det fram fungerar e-postinställningarna, och bekräftelsemejlen\n"
            "efter varje publicering kommer på samma sätt.\n\n"
            f"Skickat {datetime.now():%Y-%m-%d %H:%M} via {host}:{port}.\n",
            "plain", "utf-8",
        )
        msg["Subject"] = "Testmejl från Predikan → Podcast"
        msg["From"] = user
        msg["To"] = recipient_list
        _add_standard_headers(msg, user)
        # Mottagare som servern avvisade, med serverns svar.
        refused: dict[str, str] = {}
        try:
            code, response = server.mail(user)
            if code != 250:
                raise smtplib.SMTPSenderRefused(code, response, user)
            # Varje mottagare för sig, så att det syns vilka servern godtar.
            for address in recipients:
                code, response = server.rcpt(address)
                if code not in (250, 251):
                    refused[address] = f"{code} {response.decode('utf-8', 'replace')}"
            if len(refused) == len(recipients):
                raise smtplib.SMTPRecipientsRefused(refused)
            code, response = server.data(msg.as_bytes())
        except smtplib.SMTPSenderRefused as exc:
            steps.append(_step(
                "Skicka", False, f"Servern avvisade avsändaren {user}: {_smtp_error_text(exc)}",
                "Servern tillåter troligen bara den egna adressen som avsändare.",
            ))
            return result
        except smtplib.SMTPRecipientsRefused:
            steps.append(_step(
                "Skicka", False,
                "Servern avvisade " + "; ".join(f"{address} ({reason})" for address, reason in refused.items()),
                "Kontrollera mottagaradresserna.",
            ))
            return result
        except (smtplib.SMTPException, OSError) as exc:
            steps.append(_step("Skicka", False, f"Mejlet kunde inte skickas: {_smtp_error_text(exc)}"))
            return result

    answer = f"{code} {response.decode('utf-8', 'replace')}".strip()
    if code != 250:
        steps.append(_step("Skicka", False, f"Servern tog inte emot mejlet: {answer}"))
        return result
    accepted = [address for address in recipients if address not in refused]
    if refused:
        # Några mottagare gick bra - men inte alla.
        steps.append(_step(
            "Mottagare", None,
            "Servern avvisade " + "; ".join(f"{address} ({reason})" for address, reason in refused.items()),
            "De avvisade adresserna får inga bekräftelsemejl - kontrollera dem.",
        ))
    steps.append(_step(
        "Skicka", True, f"Servern tog emot testmejlet till {', '.join(accepted)} ({answer}).",
        "Kommer det inte fram inom några minuter har det fastnat efter servern: titta i "
        "skräpposten och i eventuell karantän/spamfilter hos mottagaren, och sök efter "
        "ämnet \"Testmejl från Predikan → Podcast\". Skickas det från en adress vars domän "
        "inte tillåter den här servern (SPF/DKIM) kan mottagaren slänga det.",
    ))
    result["ok"] = True
    result["message_id"] = msg["Message-ID"]
    return result
