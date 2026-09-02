# -*- coding: utf-8 -*-
"""Prove the mail setup works, before trusting it with a real signup.

    python check_mail.py you@example.com

Reads .env the same way the app does, reports exactly which piece is
missing or wrong, and - if everything is set - sends one real message.

WHY THIS EXISTS

Without it the only way to test mail is to sign up as a stranger and see
whether a code arrives, and when nothing arrives there is nothing to look
at: the app deliberately answers the same way whether it mailed you or
printed to a log. This says which.
"""
import io
import os
import smtplib
import sys


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def main():
    load_env()
    to = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ADMIN_EMAIL")
    if not to:
        print("Usage: python check_mail.py you@example.com")
        return 2

    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    port = os.environ.get("SMTP_PORT", "587").strip()
    sender = os.environ.get("SMTP_FROM", "").strip() or user

    print("Settings found in .env")
    print("  SMTP_HOST %s" % (host or "MISSING"))
    print("  SMTP_PORT %s" % (port or "587 (default)"))
    print("  SMTP_USER %s" % (user or "MISSING"))
    # Never print the password. Its length is enough to spot the two
    # mistakes people actually make: an empty value, and a Gmail app
    # password pasted with its spaces left in.
    if not password:
        print("  SMTP_PASS MISSING")
    else:
        print("  SMTP_PASS set, %d characters" % len(password))
        if " " in password:
            print("            ^ it contains a space. Gmail SHOWS app "
                  "passwords as 'abcd efgh ijkl mnop' but expects them "
                  "typed WITHOUT the spaces.")
    print("  SMTP_FROM %s" % (sender or "MISSING"))
    print("")

    if not (host and user and password):
        print("Not configured, so the app prints codes to its log instead.")
        print("That is a working state - nothing is broken - but nobody")
        print("can reset a password without reading the server log.")
        return 1

    print("Connecting to %s:%s ..." % (host, port))
    try:
        with smtplib.SMTP(host, int(port), timeout=15) as server:
            server.starttls()
            print("  TLS ok")
            server.login(user, password)
            print("  signed in as %s" % user)
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["Subject"] = "RandomGenerals AI - mail is working"
            msg["From"] = sender
            msg["To"] = to
            msg.set_content(
                "If you are reading this, verification codes and password "
                "reset codes will reach people.\n\n"
                "Sent by check_mail.py.")
            server.send_message(msg)
        print("  sent to %s" % to)
        print("")
        print("Mail is working. Check that inbox (and its spam folder).")
        return 0
    except smtplib.SMTPAuthenticationError as e:
        print("  REJECTED THE LOGIN: %s" % e)
        print("")
        print("For Gmail this nearly always means one of:")
        print("  - the value is your normal password, not an app password")
        print("  - 2-Step Verification is not switched on, so Google will")
        print("    not issue app passwords at all")
        print("  - the app password was pasted with its spaces")
        return 1
    except Exception as e:                          # noqa: BLE001
        print("  FAILED: %s: %s" % (type(e).__name__, e))
        print("")
        print("A timeout here is usually the host or port being wrong, or")
        print("outbound port %s being blocked on this machine." % port)
        return 1


if __name__ == "__main__":
    sys.exit(main())
