"""Credentials this server must use again, and the second factor.

TWO DIFFERENT PROBLEMS, DELIBERATELY IN ONE FILE

A password is hashed, because it is only ever compared. A provider API
key cannot be: this server has to replay it to OpenAI or Anthropic, so it
must come back out. That means encryption, and a key kept somewhere the
database is not.

That somewhere is SETTINGS_ENCRYPTION_KEY, an environment variable. A
database backup is then not a key leak, which is the entire point - the
lock and its key live in different places.

WHY cryptography IS OPTIONAL

It is deliberately not a hard dependency. This app runs on a VM that
cannot currently be redeployed, and importing a package that is not
installed would take the whole site down at boot to add a feature nobody
was using yet. So the import is lazy: without the package, storing a
provider key is refused with a sentence saying what to install, and
everything else carries on.

    pip install cryptography

TOTP is implemented here from RFC 6238 rather than pulled in, because it
is thirty lines of HMAC and a truncation rule - a specification to
follow, not cryptography to invent. The HMAC comes from hashlib.
"""
import base64
import hashlib
import hmac
import os
import struct
import time

KEY_VERSION = 1
_MISSING = ("Storing provider keys needs the 'cryptography' package. "
            "Install it on the server with: pip install cryptography")


def crypto_available():
    try:
        import cryptography.hazmat.primitives.ciphers.aead  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False


def master_key_set():
    return bool(os.environ.get("SETTINGS_ENCRYPTION_KEY", "").strip())


def unavailable_reason():
    """Why encrypted storage is off, as a sentence, or "" when it is on."""
    if not crypto_available():
        return _MISSING
    if not master_key_set():
        return ("SETTINGS_ENCRYPTION_KEY is not set on the server. "
                "Generate one with: python -c \"import os, base64; "
                "print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"")
    return ""


def available():
    return not unavailable_reason()


def _master():
    raw = os.environ.get("SETTINGS_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise RuntimeError(unavailable_reason())
    try:
        key = base64.urlsafe_b64decode(raw)
    except (ValueError, TypeError):
        raise RuntimeError("SETTINGS_ENCRYPTION_KEY is not valid base64.")
    if len(key) != 32:
        raise RuntimeError("SETTINGS_ENCRYPTION_KEY must decode to 32 bytes.")
    return key


def _aad(owner_id, label):
    """Binds a ciphertext to its owner and its purpose.

    A row copied into another account's slot then fails to decrypt rather
    than handing that account somebody else's key - so write access to
    the database is not by itself a way to move a secret around.
    """
    return ("%s|%s|%d" % (owner_id, label, KEY_VERSION)).encode("utf-8")


def encrypt(plaintext, owner_id, label):
    """-> (ciphertext, nonce). AES-256-GCM."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    nonce = os.urandom(12)
    ct = AESGCM(_master()).encrypt(nonce, plaintext.encode("utf-8"),
                                   _aad(owner_id, label))
    return ct, nonce


def decrypt(ciphertext, nonce, owner_id, label):
    """Raises if the row was altered or moved.

    GCM authenticates the ciphertext, so tampering fails loudly instead
    of yielding rubbish that would then be sent upstream as a key.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    return AESGCM(_master()).decrypt(
        bytes(nonce), bytes(ciphertext), _aad(owner_id, label)).decode("utf-8")


# ---------------------------------------------------------------- TOTP
def new_totp_secret():
    """A base32 secret, which is the format authenticator apps expect."""
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def totp_at(secret, counter, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    # RFC 4226 dynamic truncation: the low nibble of the last byte says
    # where in the digest to read the code from.
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def verify_totp(secret, code, window=1, step=30):
    """Accepts the neighbouring time steps as well as the current one.

    window=1 allows about thirty seconds of drift each way, which is
    ordinary between a phone and a server. The comparison is
    constant-time: a six-digit code is small enough that leaking where
    two strings first differ is worth avoiding.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return False
    now = int(time.time() // step)
    for drift in range(-window, window + 1):
        if hmac.compare_digest(totp_at(secret, now + drift, len(code)), code):
            return True
    return False


def provisioning_uri(secret, email, issuer="RandomGenerals AI"):
    """The otpauth:// URI an authenticator reads from a QR code."""
    from urllib.parse import quote
    label = quote("%s:%s" % (issuer, email), safe="")
    return ("otpauth://totp/%s?secret=%s&issuer=%s&digits=6&period=30"
            % (label, secret, quote(issuer, safe="")))
