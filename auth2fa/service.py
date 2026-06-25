"""The second-factor logic, shared by the demo and the Airflow integration.

Keeping enrollment and code-checking here means both front ends apply the same
rules -- in particular the replay guard -- so the demo genuinely exercises the
code path the real login uses.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

from . import totp
from .store import TotpStore

ISSUER = "SledgeHammer"

# After this many wrong codes, lock the account for the cooldown. The counter
# lives in the store (per uid), not the session, so re-entering the password
# can't reset it -- that was the gap a single session counter left open.
LOCK_THRESHOLD = 5
LOCK_COOLDOWN = 300  # seconds


def lock_remaining(store: TotpStore, uid: str, at: Optional[float] = None) -> int:
    """Seconds until `uid` may try a code again, or 0 if not locked."""
    now = time.time() if at is None else at
    lu = store.locked_until(uid)
    if lu and lu > now:
        return int(lu - now)
    return 0


def begin_enrollment(store: TotpStore, uid: str, issuer: str = ISSUER) -> Tuple[str, str]:
    """Generate a fresh secret for `uid`, save it as unconfirmed, and return
    (secret, otpauth_uri) for the enrollment page to show.
    """
    secret = totp.generate_secret()
    store.start_enrollment(uid, secret)
    return secret, totp.provisioning_uri(secret, uid, issuer=issuer)


def confirm_enrollment(store: TotpStore, uid: str, code: str,
                       at: Optional[float] = None) -> bool:
    """Finish enrollment: the user proves they scanned the QR by entering one
    code. On success the secret is marked confirmed and that step is recorded so
    the same code can't immediately be replayed as a login.
    """
    enr = store.get(uid)
    if not enr:
        return False
    matched = totp.verify(enr.secret, code, at=at)
    if matched is None:
        return False
    store.confirm(uid)
    store.record_step(uid, matched)
    return True


def check_code(store: TotpStore, uid: str, code: str,
               at: Optional[float] = None) -> bool:
    """Verify a login's second factor for an already-enrolled user.

    Rejects codes that don't match, and rejects any code from a time-step that
    has already been used (so a code sniffed once is useless for the rest of its
    30-second life). Each wrong code counts toward a per-user lockout, and while
    locked even a correct code is refused -- call lock_remaining() first to tell
    the user how long to wait.
    """
    now = time.time() if at is None else at
    if lock_remaining(store, uid, now) > 0:
        return False
    enr = store.get(uid)
    if not enr or not enr.confirmed:
        return False
    matched = totp.verify(enr.secret, code, at=now)
    if matched is None:
        # A genuinely wrong code; a replay (matched <= last_step) is handled
        # below and isn't counted, so a legit double-submit won't lock anyone.
        store.note_failure(uid, LOCK_THRESHOLD, LOCK_COOLDOWN, now)
        return False
    if enr.last_step is not None and matched <= enr.last_step:
        return False
    store.reset_failures(uid)
    store.record_step(uid, matched)
    return True
