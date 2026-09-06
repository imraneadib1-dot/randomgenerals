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
        # The connection is opened here purely to get a specific error
        # out of it. smtplib's auth failures name the actual reason;
        # mailer._send catches them and falls back to the console, which
        # is right in production and useless when diagnosing.
        with smtplib.SMTP(host, int(port), timeout=15) as server:
            server.starttls()
            print("  TLS ok")
            server.login(user, password)
            print("  signed in as %s" % user)
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

    # Now send the real thing. A hand-written test message would prove
    # the SMTP account works and nothing about the email people actually
    # receive - a broken template renders as a blank message and this
    # would still have printed "mail is working".
    try:
        import mailer
    except ImportError as e:
        print("  could not import mailer.py: %s" % e)
        return 1

    print("")
    print("Sending the real verification email (code 000000) ...")
    sent, detail = mailer.send_verification_code(
        to, "000000", 15, name=os.environ.get("ADMIN_NAME", ""))
    if not sent:
        print("  NOT SENT: %s" % detail)
        return 1
    print("  %s" % detail)
    print("")
    print("Mail is working, and that is the exact message a new signup")
    print("gets. Check the inbox - and the spam folder, since a first")
    print("message from a new sender often lands there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
