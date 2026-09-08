"""
Send the Pikachu movers report by email via Gmail SMTP.

Setup (one-time, do this before the GitHub Actions workflow can send email):

  1. Turn on 2-Step Verification on the sending Gmail account:
     https://myaccount.google.com/security
  2. Create an "App Password":
     https://myaccount.google.com/apppasswords
     - App: "Mail", device: anything (e.g. "Pikachu Tracker") -- Google gives
       you a 16-character password. This is NOT your normal Gmail password.
  3. In your GitHub repo: Settings -> Secrets and variables -> Actions ->
     "New repository secret", add three secrets:
       GMAIL_ADDRESS       the Gmail address you're sending from
       GMAIL_APP_PASSWORD  the 16-character App Password from step 2
       REPORT_RECIPIENT    the address that should receive the report
                            (can be the same as GMAIL_ADDRESS)
  4. Locally, set the same three as environment variables if you want to
     test sending outside of GitHub Actions -- never hardcode them in code.

Credentials are read only from the environment. Nothing is ever hardcoded.
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

logger = logging.getLogger(__name__)


class EmailConfigError(RuntimeError):
    """Raised when required email environment variables are missing."""


def send_report_email(html_body, subject="Pikachu Monthly Price Movers"):
    """Send html_body as an HTML email using Gmail SMTP.

    Reads GMAIL_ADDRESS, GMAIL_APP_PASSWORD, REPORT_RECIPIENT from the
    environment. Raises EmailConfigError if any are missing -- callers that
    want to skip sending when unconfigured (e.g. local runs) should check
    for that up front instead of relying on the exception, see
    generate_report.py.
    """
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("REPORT_RECIPIENT")

    missing = [
        name for name, value in [
            ("GMAIL_ADDRESS", gmail_address),
            ("GMAIL_APP_PASSWORD", gmail_app_password),
            ("REPORT_RECIPIENT", recipient),
        ] if not value
    ]
    if missing:
        raise EmailConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}"
        )

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = gmail_address
    message["To"] = recipient
    message.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [recipient], message.as_string())

    logger.info("sent report email to %s", recipient)


def is_email_configured():
    """True if all three required environment variables are set."""
    return all(
        os.environ.get(name)
        for name in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "REPORT_RECIPIENT")
    )
