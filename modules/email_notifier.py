"""
Modul: email_notifier
Skickar ett bekräftelsemail när ett avsnitt har publicerats.
Om EMAIL_ENABLED=false (standard) skickas inget mail - istället visar
frontend en sammanfattningssida med samma information.
"""
import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config
from modules import text_formatting


def _format_duration(total_seconds: float | None) -> str:
    """Formaterar sekunder som en läsbar sträng, t.ex. '12min 4s' eller '1h 3min'."""
    if total_seconds is None:
        return "-"
    seconds = max(0, int(round(total_seconds)))
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
    """
    if not config.EMAIL_ENABLED:
        return False

    if not all([config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD, config.NOTIFY_EMAIL]):
        raise RuntimeError(
            "EMAIL_ENABLED=true men SMTP-uppgifter saknas i .env "
            "(SMTP_HOST, SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL)."
        )

    tags_text = ", ".join(tags) if tags else "-"
    duration_text = _format_duration(processing_seconds)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Nytt avsnitt publicerat: {title}"
    msg["From"] = config.SMTP_USER
    msg["To"] = config.NOTIFY_EMAIL

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

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
        server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)

    return True
