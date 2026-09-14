"""
Modul: email_notifier
Skickar ett bekräftelsemail när ett avsnitt har publicerats.
Om EMAIL_ENABLED=false (standard) skickas inget mail - istället visar
frontend en sammanfattningssida med samma information.
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import config


def send_publish_confirmation(
    title: str,
    speaker: str,
    description: str,
    episode_url: str,
) -> bool:
    """
    Skickar ett bekräftelsemail. Returnerar True om mailet skickades,
    False om e-post är avaktiverat i konfigurationen.
    """
    if not config.EMAIL_ENABLED:
        return False

    if not all([config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD, config.NOTIFY_EMAIL]):
        raise RuntimeError(
            "EMAIL_ENABLED=true men SMTP-uppgifter saknas i .env "
            "(SMTP_HOST, SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL)."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Nytt avsnitt publicerat: {title}"
    msg["From"] = config.SMTP_USER
    msg["To"] = config.NOTIFY_EMAIL

    text_body = f"""Ett nytt avsnitt har publicerats!

Titel: {title}
Talare: {speaker}
Länk: {episode_url}

Beskrivning:
{description}
"""

    html_body = f"""
    <html>
      <body style="font-family: sans-serif; color: #222;">
        <h2>🎙️ Nytt avsnitt publicerat</h2>
        <p><strong>Titel:</strong> {title}</p>
        <p><strong>Talare:</strong> {speaker}</p>
        <p><strong>Länk:</strong> <a href="{episode_url}">{episode_url}</a></p>
        <p><strong>Beskrivning:</strong><br>{description}</p>
      </body>
    </html>
    """

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
        server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)

    return True
