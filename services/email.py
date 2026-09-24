"""
Outbound email — currently just the OTP login code.

Plain smtplib over STARTTLS rather than a provider SDK, so switching from
Gmail's SMTP to a transactional-email provider later is a config.py change,
not a code change: every provider that can send email at all speaks SMTP.
"""
import smtplib
from email.message import EmailMessage

import config


class EmailNotConfigured(Exception):
    """SMTP_HOST is blank -- the feature that tried to send is disabled."""


def send_email(to: str, subject: str, body: str) -> None:
    """One plain-text email. Raises EmailNotConfigured or smtplib's own errors
    -- callers decide how those become an HTTP response; this layer only sends.
    """
    if not config.SMTP_HOST:
        raise EmailNotConfigured(
            "Email is not configured on this server (SMTP_HOST is blank in "
            ".env), so no code can be sent."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg.set_content(body)

    # STARTTLS on the standard submission port (587), which is what every
    # mainstream provider's SMTP relay expects. SMTP_SSL (port 465, TLS from
    # the first byte) is not offered here -- 587 is enough for every provider
    # in the docstring above, and one less branch is one less way to
    # misconfigure it.
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
        server.starttls()
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)
