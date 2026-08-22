"""Sends the email-verification code, or tells you it couldn't.

Real delivery needs real credentials - an SMTP account this app can log
into - which is not something that can be invented. Set these in .env to
turn on actual sending:

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=you@gmail.com
    SMTP_PASS=an app password, not your login password
    SMTP_FROM=you@gmail.com

(Gmail specifically requires an "app password" - Google Account settings
-> Security -> 2-Step Verification -> App passwords - because it no
longer accepts a plain account password over SMTP.)

Without those set, the code is printed to the server console instead.
The signup flow works exactly the same either way; only where the code
shows up changes. That fallback is not a placeholder to delete later -
it's what makes this testable right now, before anyone has configured
a mail account.
"""
import os
import smtplib
from email.message import EmailMessage


def _smtp_config():
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not (host and user and password):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "from_addr": os.environ.get("SMTP_FROM", user),
    }


def send_verification_code(to_email, code):
    """Returns (sent, detail). `sent` is False in console-fallback mode -
    that's expected, not an error, so callers shouldn't treat it as one."""
    cfg = _smtp_config()

    if not cfg:
        print(f"\n{'=' * 50}\n"
             f"  [DEV] No SMTP configured - verification code for\n"
             f"  {to_email}:\n\n"
             f"      {code}\n\n"
             f"  Set SMTP_HOST/SMTP_USER/SMTP_PASS in .env to send\n"
             f"  this for real instead. See mailer.py.\n"
             f"{'=' * 50}\n")
        return False, "printed to server console (no SMTP configured)"

    msg = EmailMessage()
    msg["Subject"] = "Your verification code"
    msg["From"] = cfg["from_addr"]
    msg["To"] = to_email
    msg.set_content(
        f"Your verification code is: {code}\n\n"
        f"It expires in 10 minutes. If you didn't request this, ignore it."
    )

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as server:
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        return True, f"sent to {to_email}"
    except (smtplib.SMTPException, OSError) as e:
        print(f"[mailer] SMTP send failed, falling back to console: {e}")
        print(f"[DEV] verification code for {to_email}: {code}")
        return False, f"SMTP send failed ({e}); printed to server console instead"
