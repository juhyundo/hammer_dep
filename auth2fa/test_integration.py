"""End-to-end test of the FAB login override (auth2fa.fab_integration).

Builds a minimal Flask-AppBuilder app with a mocked LDAP backend and a throwaway
SQLite TOTP store, then drives the real /login -> /mfa -> login_user flow through
the routes. This exercises the same view code the live Airflow deployment runs.

Run: .venv/bin/python -m pytest auth2fa/test_integration.py -q
(or plain: .venv/bin/python auth2fa/test_integration.py)
"""
import os
import tempfile
import time
import warnings

import pytest

warnings.filterwarnings("ignore")


def _build_app():
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy
    from flask_appbuilder import AppBuilder
    from flask_appbuilder.security.manager import AUTH_LDAP
    from flask_appbuilder.security.sqla.manager import SecurityManager

    from auth2fa import fab_integration as fi
    from auth2fa.store import SqliteTotpStore

    totp_db = tempfile.mktemp(suffix=".sqlite")
    meta_db = tempfile.mktemp(suffix=".db")
    fi._store = SqliteTotpStore(totp_db)

    class TestSM(SecurityManager):
        authldapview = fi.TwoFactorAuthLDAPView

        def auth_user_ldap(self, username, password):
            if password != "good":
                return None
            u = self.find_user(username=username)
            if not u:
                u = self.add_user(username=username, first_name="T", last_name="U",
                                  email=username + "@example.edu", role=self.find_role("Admin"))
            return u

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="testkey",
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{meta_db}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        AUTH_TYPE=AUTH_LDAP,
        AUTH_LDAP_SERVER="ldaps://unused.example",
        WTF_CSRF_ENABLED=False,
        AUTH_USER_REGISTRATION=True,
        AUTH_USER_REGISTRATION_ROLE="Admin",
    )
    db = SQLAlchemy(app)
    with app.app_context():
        AppBuilder(app, db.session, security_manager_class=TestSM)
    return app, fi, totp_db, meta_db


@pytest.fixture
def app_ctx():
    app, fi, totp_db, meta_db = _build_app()
    yield app, fi
    for p in (totp_db, meta_db):
        if os.path.exists(p):
            os.unlink(p)


def test_routes_registered(app_ctx):
    app, _ = app_ctx
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert any(r.endswith("/login/") for r in rules)
    assert any(r.endswith("/mfa/") for r in rules)


def test_full_flow(app_ctx):
    from auth2fa import totp
    app, fi = app_ctx
    c = app.test_client()

    # login page renders
    assert c.get("/login/").status_code == 200

    # wrong password never reaches the second factor
    r = c.post("/login/", data={"username": "alice", "password": "bad"})
    assert "/mfa/" not in r.headers.get("Location", "")

    # right password parks the user and redirects to the second factor
    r = c.post("/login/", data={"username": "alice", "password": "good"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/mfa/")

    # first time: enrollment page with a QR
    r = c.get("/mfa/")
    assert r.status_code == 200 and b"Set up two-factor" in r.data and b"data:image/png" in r.data

    # confirming the current code finishes enrollment and logs in
    secret = fi._store.get("alice").secret
    r = c.post("/mfa/", data={"code": totp.totp(secret)})
    assert r.status_code == 302 and fi._store.is_enrolled("alice")

    # returning user gets the verify page, not enrollment
    c.get("/logout/")
    c.post("/login/", data={"username": "alice", "password": "good"})
    r = c.get("/mfa/")
    assert b"Two-factor code" in r.data and b"Set up two-factor" not in r.data

    # wrong code stays, a fresh next-window code logs in
    c.post("/mfa/", data={"code": "000000"})
    code_next = totp.totp(secret, at=time.time() + 30)
    assert c.post("/mfa/", data={"code": code_next}).status_code == 302

    # replay of that same code is rejected
    c.get("/logout/")
    c.post("/login/", data={"username": "alice", "password": "good"})
    r = c.post("/mfa/", data={"code": code_next})
    assert r.status_code == 200 and b"match" in r.data


def test_lockout_survives_relogin(app_ctx):
    # The exploit the review found: re-POSTing /login reset a session-based
    # attempt counter, so an attacker with the password could brute-force the
    # code indefinitely. The lockout now lives server-side per uid, so logging
    # in again must NOT hand out a fresh guessing budget.
    from auth2fa import totp, service
    app, fi = app_ctx
    c = app.test_client()

    # enroll frank (GET /mfa/ creates the pending secret)
    c.post("/login/", data={"username": "frank", "password": "good"})
    c.get("/mfa/")
    secret = fi._store.get("frank").secret
    c.post("/mfa/", data={"code": totp.totp(secret)})
    assert fi._store.is_enrolled("frank")
    c.get("/logout/")

    # pick a code outside a generous window so it always counts as wrong
    now = time.time()
    valid = {totp.totp(secret, at=now + off * 30) for off in range(-2, 3)}
    wrong = next(x for x in ("000000", "111111", "222222", "333333", "444444") if x not in valid)

    # brute-force using the old reset trick: login, guess, login, guess, ...
    for _ in range(service.LOCK_THRESHOLD):
        c.post("/login/", data={"username": "frank", "password": "good"})
        c.post("/mfa/", data={"code": wrong})

    # the account is locked server-side; a fresh login does not clear it
    c.post("/login/", data={"username": "frank", "password": "good"})
    assert service.lock_remaining(fi._store, "frank") > 0
    # even the correct current code is refused while locked
    r = c.post("/mfa/", data={"code": totp.totp(secret, at=time.time() + 30)})
    assert r.status_code == 200 and b"Too many" in r.data


def test_mfa_without_pending_redirects_to_login(app_ctx):
    app, _ = app_ctx
    c = app.test_client()
    r = c.get("/mfa/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login/")


def test_logout_clears_pending(app_ctx):
    # A user who passes the password but logs out before the code must not leave
    # a pending user id parked in the session.
    app, _ = app_ctx
    c = app.test_client()
    c.post("/login/", data={"username": "dave", "password": "good"})  # pending set
    assert c.get("/mfa/").status_code == 200  # would show the factor page
    c.get("/logout/")
    r = c.get("/mfa/")  # pending cleared -> bounced to login
    assert r.status_code == 302 and r.headers["Location"].endswith("/login/")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
