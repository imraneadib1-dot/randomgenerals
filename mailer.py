"""The emails this app sends, and how they get out.

Two messages: an address-verification code and a password-reset code.
Both go out as multipart/alternative - a plain-text part and an HTML
part - because a mail client picks whichever it can render and a
plain-text-only message from a paid product reads as an afterthought.

WHY THE HTML IS WRITTEN THE WAY IT IS

Email clients are not browsers. Gmail strips <style> blocks, Outlook
renders through Word, and neither supports flexbox, grid, or web fonts.
So every rule here is inline, the layout is a table, and the fonts are
the ones already on the machine. It looks plain by web standards on
purpose: the alternative is a layout that collapses in Outlook.

DELIVERY

Real sending needs an SMTP account, set in .env:

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=you@gmail.com
    SMTP_PASS=an app password, not your login password
    SMTP_FROM=you@gmail.com
    MAIL_FROM_NAME=RandomGenerals     (optional display name)

Gmail requires an app password - Google Account, Security, 2-Step
Verification, App passwords - because it no longer accepts a plain
account password over SMTP.

Without those set, the code is printed to the server console instead.
The flow works identically either way; only where the code appears
changes. That fallback is not a placeholder: it is what makes this
testable before anyone has configured a mail account.
"""
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

PRODUCT = "RandomGenerals"
SITE_URL = "https://randomgenerals.com"

# The palette from the app's own stylesheet, so an email looks like it
# came from the same product as the page it links to.
INK = "#1b2430"
MUTED = "#61707f"
BRONZE = "#6e6420"
RULE = "#e3e1d8"
PAPER = "#f6f5f0"


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
        "from_name": os.environ.get("MAIL_FROM_NAME", PRODUCT),
    }


def _greeting(name):
    """"Hi Sam," or "Hello," - never "Hi ," from an empty name."""
    first = (name or "").strip().split(" ")[0]
    return "Hi %s," % first if first else "Hello,"


def _html(greeting, intro, code, ttl_minutes, closing, footnote):
    """One template for both messages.

    A table rather than divs, and every style inline. Outlook renders
    mail through Word, which ignores stylesheets and most modern CSS -
    so anything not written this way arrives unstyled or broken.
    """
    return """\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:%(paper)s;">
    <table role="presentation" width="100%%" cellpadding="0" cellspacing="0"
           style="background:%(paper)s;padding:32px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%%" cellpadding="0"
                 cellspacing="0"
                 style="max-width:520px;background:#ffffff;
                        border:1px solid %(rule)s;border-radius:12px;">
            <tr>
              <td style="padding:28px 32px 8px;">
                <p style="margin:0;font:600 15px/1.3 -apple-system,
                          BlinkMacSystemFont,'Segoe UI',Roboto,Arial,
                          sans-serif;color:%(bronze)s;
                          letter-spacing:0.02em;">%(product)s</p>
              </td>
            </tr>
            <tr>
              <td style="padding:0 32px;">
                <hr style="border:none;border-top:1px solid %(rule)s;
                           margin:12px 0 22px;" />
                <p style="margin:0 0 14px;font:400 16px/1.6 -apple-system,
                          BlinkMacSystemFont,'Segoe UI',Roboto,Arial,
                          sans-serif;color:%(ink)s;">%(greeting)s</p>
                <p style="margin:0 0 22px;font:400 16px/1.6 -apple-system,
                          BlinkMacSystemFont,'Segoe UI',Roboto,Arial,
                          sans-serif;color:%(ink)s;">%(intro)s</p>
              </td>
            </tr>
            <tr>
              <td align="center" style="padding:0 32px;">
                <table role="presentation" cellpadding="0" cellspacing="0"
                       style="background:%(paper)s;border:1px solid %(rule)s;
                              border-radius:10px;">
                  <tr>
                    <td style="padding:18px 34px;font:600 32px/1
                               'SF Mono',Menlo,Consolas,monospace;
                               color:%(ink)s;letter-spacing:0.22em;">
                      %(code)s
                    </td>
                  </tr>
                </table>
                <p style="margin:12px 0 0;font:400 13px/1.5 -apple-system,
                          BlinkMacSystemFont,'Segoe UI',Roboto,Arial,
                          sans-serif;color:%(muted)s;">
                  This code expires in %(ttl)d minutes and can be used once.
                </p>
              </td>
            </tr>
            <tr>
              <td style="padding:24px 32px 4px;">
                <p style="margin:0 0 18px;font:400 15px/1.6 -apple-system,
                          BlinkMacSystemFont,'Segoe UI',Roboto,Arial,
                          sans-serif;color:%(ink)s;">%(closing)s</p>
                <hr style="border:none;border-top:1px solid %(rule)s;
                           margin:0 0 16px;" />
                <p style="margin:0 0 6px;font:400 13px/1.6 -apple-system,
                          BlinkMacSystemFont,'Segoe UI',Roboto,Arial,
                          sans-serif;color:%(muted)s;">%(footnote)s</p>
                <p style="margin:0 0 24px;font:400 13px/1.6 -apple-system,
                          BlinkMacSystemFont,'Segoe UI',Roboto,Arial,
                          sans-serif;color:%(muted)s;">
                  &mdash; The %(product)s team &middot;
                  <a href="%(site)s"
                     style="color:%(bronze)s;text-decoration:none;">
                    randomgenerals.com</a>
                </p>
              </td>
            </tr>
          </table>
          <p style="margin:16px 0 0;font:400 12px/1.5 -apple-system,
                    BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
                    color:%(muted)s;">
            This message was sent to you because someone entered this
            address at %(site_short)s.
          </p>
        </td>
      </tr>
    </table>
  </body>
</html>""" % {
        "product": PRODUCT, "site": SITE_URL, "site_short": "randomgenerals.com",
        "greeting": greeting, "intro": intro, "code": code,
        "ttl": ttl_minutes, "closing": closing, "footnote": footnote,
        "ink": INK, "muted": MUTED, "bronze": BRONZE, "rule": RULE,
        "paper": PAPER,
    }


def _send(to_email, subject, text_body, html_body, log_label, code):
    """Send, or print to the console when SMTP is not configured.

    -> (sent, detail). `sent` is False in console-fallback mode, which is
    expected rather than an error, so callers must not treat it as one.
    """
    cfg = _smtp_config()

    if not cfg:
        bar = "=" * 54
        print("\n%s\n  [DEV] No SMTP configured - %s for\n  %s:\n\n"
              "      %s\n\n  Set SMTP_HOST/SMTP_USER/SMTP_PASS in .env to "
              "send\n  this for real instead. See mailer.py.\n%s\n"
              % (bar, log_label, to_email, code, bar))
        return False, "printed to server console (no SMTP configured)"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_addr"]))
    msg["To"] = to_email
    # Date and Message-ID are set explicitly: some spam filters score a
    # message down for missing them, and a verification code landing in
    # spam is the same as not sending it.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="randomgenerals.com")
    # These are transactional, not marketing. Saying so stops well-behaved
    # clients offering an unsubscribe that would break sign-in.
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        return True, "sent to %s" % to_email
    except (smtplib.SMTPException, OSError) as e:
        print("[mailer] send failed, falling back to console: %s" % e)
        print("[DEV] %s for %s: %s" % (log_label, to_email, code))
        return False, "SMTP send failed (%s); printed to the console" % e


def send_verification_code(to_email, code, ttl_minutes=15, name=""):
    """Confirm that an address belongs to the person using it."""
    greeting = _greeting(name)
    intro = ("Please use the code below to confirm your email address. "
             "Verifying it is what lets you reset your password later if "
             "you ever need to.")
    closing = ("If you did not create an account, you can safely ignore "
               "this message and nothing further will happen.")
    footnote = ("For your security, never share this code. We will never "
                "ask you for it by email, chat or phone.")

    text = (
        "%s\n\n"
        "Please use the code below to confirm your email address.\n\n"
        "    %s\n\n"
        "This code expires in %d minutes and can be used once.\n\n"
        "Verifying your address is what lets you reset your password "
        "later if you ever need to.\n\n"
        "If you did not create an account, you can safely ignore this "
        "message and nothing further will happen.\n\n"
        "For your security, never share this code. We will never ask you "
        "for it by email, chat or phone.\n\n"
        "-- The %s team\n%s\n"
        % (greeting, code, ttl_minutes, PRODUCT, SITE_URL))

    return _send(
        to_email,
        "%s is your %s verification code" % (code, PRODUCT),
        text,
        _html(greeting, intro, code, ttl_minutes, closing, footnote),
        "verification code", code)


def send_reset_code(to_email, code, ttl_minutes=15, name=""):
    """Let someone locked out set a new password.

    Worded more carefully than the verification mail: this is the message
    that arrives unrequested when somebody is trying to take an account,
    so it has to make clear that nothing has changed yet and that
    ignoring it is the right response.
    """
    greeting = _greeting(name)
    intro = ("We received a request to reset the password for your "
             "account. Use the code below to choose a new one.")
    closing = ("If you did not request this, no action is needed. Your "
               "password has not been changed, and it cannot be changed "
               "without this code.")
    footnote = ("For your security, never share this code. We will never "
                "ask you for it by email, chat or phone.")

    text = (
        "%s\n\n"
        "We received a request to reset the password for your account.\n"
        "Use the code below to choose a new one.\n\n"
        "    %s\n\n"
        "This code expires in %d minutes and can be used once.\n\n"
        "If you did not request this, no action is needed. Your password "
        "has not been changed, and it cannot be changed without this "
        "code.\n\n"
        "For your security, never share this code. We will never ask you "
        "for it by email, chat or phone.\n\n"
        "-- The %s team\n%s\n"
        % (greeting, code, ttl_minutes, PRODUCT, SITE_URL))

    return _send(
        to_email,
        "%s is your %s password reset code" % (code, PRODUCT),
        text,
        _html(greeting, intro, code, ttl_minutes, closing, footnote),
        "PASSWORD RESET code", code)
