"""Tests for the TOTP core and the enrollment/verify service.

Run with: .venv/bin/python -m pytest auth2fa/test_totp.py -q
(or plain: .venv/bin/python auth2fa/test_totp.py)
"""
import base64
import os
import tempfile

from auth2fa import service, totp
from auth2fa.store import SqliteTotpStore

# RFC 6238 Appendix B reference secret: ASCII "12345678901234567890".
_RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")

# The published SHA1 reference codes, truncated to the 6 digits we use.
_RFC_VECTORS = {
    59: "287082",
    1111111109: "081804",
    1111111111: "050471",
    1234567890: "005924",
    2000000000: "279037",
    20000000000: "353130",
}


def test_rfc6238_vectors():
    for t, expected in _RFC_VECTORS.items():
        assert totp.totp(_RFC_SECRET, at=t) == expected, f"t={t}"


def test_base32_secret_roundtrip():
    s = totp.generate_secret()
    assert s == s.upper() and "=" not in s
    # decodes without error and is the requested length (20 bytes -> 32 chars)
    assert len(totp._decode_secret(s)) == 20


def test_provisioning_uri_is_canonical():
    # Literal colon in the label (not %3A) and a base32 secret, so Microsoft
    # Authenticator and friends all parse it. The default SHA1/6/30 parameters
    # are omitted to keep the URI short (and the QR low-density).
    from urllib.parse import urlsplit, parse_qs
    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, "lawrencejrhee", issuer="SledgeHammer")
    u = urlsplit(uri)
    assert u.scheme == "otpauth" and u.netloc == "totp"
    assert u.path == "/SledgeHammer:lawrencejrhee"  # literal colon, no %3A
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert q == {"secret": secret, "issuer": "SledgeHammer"}  # no default params
    # the secret carried in the URI generates codes that verify
    assert totp.verify(q["secret"], totp.totp(q["secret"])) is not None
    # non-default settings ARE spelled out so they aren't silently lost
    custom = totp.provisioning_uri(secret, "x", period=60, digits=8)
    cq = parse_qs(urlsplit(custom).query)
    assert cq["period"] == ["60"] and cq["digits"] == ["8"]


def test_verify_returns_matching_step():
    t = 59
    code = totp.totp(_RFC_SECRET, at=t)
    assert totp.verify(_RFC_SECRET, code, at=t) == totp.step_at(t)


def test_verify_window_accepts_neighbor_step():
    t = 1000 * 30  # a clean step boundary
    prev = totp.totp(_RFC_SECRET, at=t - 30)
    nxt = totp.totp(_RFC_SECRET, at=t + 30)
    assert totp.verify(_RFC_SECRET, prev, at=t, window=1) is not None
    assert totp.verify(_RFC_SECRET, nxt, at=t, window=1) is not None
    # outside the window it's rejected
    far = totp.totp(_RFC_SECRET, at=t + 120)
    assert totp.verify(_RFC_SECRET, far, at=t, window=1) is None


def test_verify_rejects_malformed():
    for bad in ("", "12345", "1234567", "abcdef", "12 34 56"):
        assert totp.verify(_RFC_SECRET, bad, at=59) is None


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.unlink(path)
    return SqliteTotpStore(path), path


def test_service_enroll_then_verify():
    store, path = _fresh_store()
    try:
        secret, uri = service.begin_enrollment(store, "alice")
        assert uri.startswith("otpauth://totp/") and secret in uri
        assert not store.is_enrolled("alice")  # pending until confirmed
        # a wrong code does not confirm
        assert service.confirm_enrollment(store, "alice", "000000") is False
        # the right code confirms
        code = totp.totp(secret)
        assert service.confirm_enrollment(store, "alice", code) is True
        assert store.is_enrolled("alice")
    finally:
        os.path.exists(path) and os.unlink(path)


def test_service_replay_rejected():
    store, path = _fresh_store()
    try:
        secret, _ = service.begin_enrollment(store, "bob")
        # confirm at a fixed step, then a later step logs in once
        t0 = 5_000 * 30
        service.confirm_enrollment(store, "bob", totp.totp(secret, at=t0), at=t0)
        t1 = t0 + 30
        code = totp.totp(secret, at=t1)
        assert service.check_code(store, "bob", code, at=t1) is True
        # same code, same step -> replay, rejected
        assert service.check_code(store, "bob", code, at=t1) is False
        # a fresh later code still works
        t2 = t1 + 30
        assert service.check_code(store, "bob", totp.totp(secret, at=t2), at=t2) is True
    finally:
        os.path.exists(path) and os.unlink(path)


def test_service_lockout():
    store, path = _fresh_store()
    try:
        secret, _ = service.begin_enrollment(store, "dave")
        t0 = 9_000 * 30
        service.confirm_enrollment(store, "dave", totp.totp(secret, at=t0), at=t0)
        t = t0 + 30
        valid_now = totp.totp(secret, at=t)
        wrong = "654321" if valid_now != "654321" else "123456"
        # wrong codes accumulate; at the threshold the account locks
        for _ in range(service.LOCK_THRESHOLD):
            assert service.check_code(store, "dave", wrong, at=t) is False
        assert service.lock_remaining(store, "dave", at=t) > 0
        # while locked, even the correct code is refused
        assert service.check_code(store, "dave", valid_now, at=t) is False
        # after the cooldown, a correct code works and clears the lock
        t2 = t + service.LOCK_COOLDOWN + 1
        assert service.lock_remaining(store, "dave", at=t2) == 0
        assert service.check_code(store, "dave", totp.totp(secret, at=t2), at=t2) is True
        assert store.locked_until("dave") is None
    finally:
        os.path.exists(path) and os.unlink(path)


def test_replay_does_not_count_toward_lockout():
    store, path = _fresh_store()
    try:
        secret, _ = service.begin_enrollment(store, "erin")
        t0 = 11_000 * 30
        service.confirm_enrollment(store, "erin", totp.totp(secret, at=t0), at=t0)
        t = t0 + 30
        code = totp.totp(secret, at=t)
        assert service.check_code(store, "erin", code, at=t) is True
        # replaying the same code many times must not trip the lockout
        for _ in range(service.LOCK_THRESHOLD + 2):
            assert service.check_code(store, "erin", code, at=t) is False
        assert service.lock_remaining(store, "erin", at=t) == 0
    finally:
        os.path.exists(path) and os.unlink(path)


def test_service_check_requires_confirmed():
    store, path = _fresh_store()
    try:
        secret, _ = service.begin_enrollment(store, "carol")  # pending, not confirmed
        assert service.check_code(store, "carol", totp.totp(secret)) is False
    finally:
        os.path.exists(path) and os.unlink(path)


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"\n{len(fns)} tests passed")
