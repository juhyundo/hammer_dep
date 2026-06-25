"""Time-based one-time passwords, built on the standard library.

This is the second authentication factor for the Airflow login. The first
factor is the user's EECS password (LDAP, see webserver_config.py); this adds a
rotating six-digit code from an authenticator app on the user's phone.

There's no third-party dependency here on purpose. A TOTP code is just an
HMAC-SHA1 of the current 30-second time window, keyed by a per-user secret, with
the last few bits used to pick six digits out of the digest (RFC 4226 for the
HOTP math, RFC 6238 for the time-window part). Google Authenticator, Authy,
1Password, and Microsoft Authenticator all implement the same scheme, so a
secret generated here pairs with whichever app the user already has.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote, urlencode

DEFAULT_DIGITS = 6
DEFAULT_PERIOD = 30

# Map the otpauth "algorithm" name to the hashlib constructor. Authenticator
# apps default to SHA1; the others exist for completeness but SHA1 is what every
# app supports without fuss.
_ALGORITHMS = {
    "SHA1": hashlib.sha1,
    "SHA256": hashlib.sha256,
    "SHA512": hashlib.sha512,
}


def generate_secret(num_bytes: int = 20) -> str:
    """Return a fresh base32 secret with the padding stripped.

    20 bytes is the size RFC 4226 recommends and what most apps expect. The
    base32 alphabet (no padding) is the format the otpauth URI and manual-entry
    boxes in authenticator apps use.
    """
    raw = secrets.token_bytes(num_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    """Turn a base32 secret string back into key bytes.

    Tolerates the spaces and lowercase that show up when someone types a secret
    in by hand, and re-adds the '=' padding base64.b32decode insists on.
    """
    s = secret.strip().replace(" ", "").upper()
    s += "=" * ((-len(s)) % 8)
    return base64.b32decode(s)


def hotp(secret: str, counter: int, digits: int = DEFAULT_DIGITS,
         algorithm: str = "SHA1") -> str:
    """The counter-based code (RFC 4226). TOTP is this with counter = time/period."""
    key = _decode_secret(secret)
    mac = hmac.new(key, struct.pack(">Q", counter), _ALGORITHMS[algorithm.upper()]).digest()
    offset = mac[-1] & 0x0F
    truncated = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** digits)).zfill(digits)


def step_at(at: Optional[float] = None, period: int = DEFAULT_PERIOD) -> int:
    """Which time-step (counter) a given unix time falls in."""
    now = time.time() if at is None else at
    return int(now // period)


def totp(secret: str, at: Optional[float] = None, digits: int = DEFAULT_DIGITS,
         period: int = DEFAULT_PERIOD, algorithm: str = "SHA1") -> str:
    """The code an authenticator app shows right now (or at unix time `at`)."""
    return hotp(secret, step_at(at, period), digits=digits, algorithm=algorithm)


def verify(secret: str, code: str, at: Optional[float] = None,
           digits: int = DEFAULT_DIGITS, period: int = DEFAULT_PERIOD,
           algorithm: str = "SHA1", window: int = 1) -> Optional[int]:
    """Check a code; return the time-step it matched, or None if it didn't.

    The window lets a code from one step on either side pass, which covers a
    phone clock that's a few seconds off from the server. Returning the matched
    step (rather than just True) is what makes replay protection possible: the
    caller stores the step and refuses to accept that same step again, so a code
    that's been used once can't be replayed during the rest of its 30 seconds.

    Comparison is constant-time so a network attacker can't learn the right code
    digit by digit from response timing.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != digits:
        return None
    base = step_at(at, period)
    for offset in range(-window, window + 1):
        counter = base + offset
        if counter < 0:
            continue
        if hmac.compare_digest(hotp(secret, counter, digits=digits, algorithm=algorithm), code):
            return counter
    return None


def provisioning_uri(secret: str, account_name: str, issuer: str = "SledgeHammer",
                     digits: int = DEFAULT_DIGITS, period: int = DEFAULT_PERIOD,
                     algorithm: str = "SHA1") -> str:
    """Build the otpauth:// URI an authenticator app reads from a QR code.

    The label carries both issuer and account so the app shows
    "SledgeHammer (lawrencejrhee)" rather than a bare username.
    """
    # Encode the issuer and account separately and keep the ":" between them
    # literal. Quoting the whole "issuer:account" turns the colon into %3A, which
    # Google Authenticator tolerates but Microsoft Authenticator does not parse
    # reliably. This literal-colon form is the canonical otpauth label.
    label = f"{quote(issuer, safe='')}:{quote(account_name, safe='')}"
    params = {"secret": secret, "issuer": issuer}
    # Only spell out the optional parameters when they differ from the universal
    # defaults (SHA1 / 6 digits / 30s). Leaving the defaults out keeps the URI
    # short, which keeps the QR low-density and easy for a phone camera to read,
    # and sidesteps the extra params some authenticator apps are fussy about.
    if algorithm.upper() != "SHA1":
        params["algorithm"] = algorithm.upper()
    if digits != DEFAULT_DIGITS:
        params["digits"] = str(digits)
    if period != DEFAULT_PERIOD:
        params["period"] = str(period)
    return f"otpauth://totp/{label}?{urlencode(params)}"
