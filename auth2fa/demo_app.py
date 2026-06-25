"""A standalone, runnable demo of the LDAP + TOTP two-factor login.

This stands apart from Airflow on purpose: it lets you click through the whole
flow -- password, QR enrollment, six-digit code -- in a browser without touching
the live deployment. The second-factor logic (auth2fa.service) is exactly what
the real Airflow integration uses, so what you see here is what you'd get there.

Run it:

    cd /bwrcq/home/lawrencejrhee/hammer
    .venv/bin/python -m auth2fa.demo_app --port 8099

Then open http://<this-host>:8099/ (tunnel the port if your browser is
elsewhere: ssh -L 8099:localhost:8099 <user>@<this-host>).

Password check:
  - default: any non-empty password is accepted. The demo is about the SECOND
    factor, so this keeps the first one out of the way -- pick any username.
  - SLEDGE_2FA_DEMO_LDAP=1: check the password against real EECS LDAP instead,
    the same anonymous search-and-bind the deployment uses. Use your own EECS
    uid/password.
"""
from __future__ import annotations

import argparse
import os
import secrets as _secrets

from flask import (
    Flask, Response, redirect, render_template_string, request, session, url_for,
)

from . import service
from .qr import qr_img
from .store import get_store

app = Flask(__name__)
app.secret_key = os.environ.get("SLEDGE_2FA_DEMO_SECRET") or _secrets.token_hex(32)

# The demo always uses the file-backed SQLite store so it needs no database.
_store = get_store("sqlite")


def _password_ok(uid: str, password: str) -> bool:
    """First factor. Real LDAP when asked, otherwise accept any non-empty password."""
    if not uid or not password:
        return False
    if os.environ.get("SLEDGE_2FA_DEMO_LDAP") == "1":
        return _ldap_check(uid, password)
    return True


def _ldap_check(uid: str, password: str) -> bool:
    """Anonymous search-and-bind against EECS LDAP, mirroring webserver_config.py."""
    import ldap  # python-ldap
    import ldap.filter
    server = os.environ.get("AUTH_LDAP_SERVER", "ldaps://ldap.eecs.berkeley.edu")
    base = os.environ.get("AUTH_LDAP_SEARCH", "dc=eecs,dc=berkeley,dc=edu")
    try:
        con = ldap.initialize(server)
        con.simple_bind_s()  # anonymous
        flt = "(uid=%s)" % ldap.filter.escape_filter_chars(uid)
        res = con.search_s(base, ldap.SCOPE_SUBTREE, flt, ["dn"])
        if not res:
            return False
        user_dn = res[0][0]
        con2 = ldap.initialize(server)
        con2.simple_bind_s(user_dn, password)  # raises on wrong password
        con2.unbind_s()
        return True
    except ldap.LDAPError:
        return False


# ---- templates ------------------------------------------------------------

_BASE = """
<!doctype html>
<title>SledgeHammer 2FA demo</title>
<style>
  :root { color-scheme: light; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 30rem;
         margin: 3rem auto; padding: 0 1rem; line-height: 1.5;
         background: #fff; color: #1a1a1a; }
  h1 { font-size: 1.3rem; }
  .card { border: 1px solid #8884; border-radius: 12px; padding: 1.5rem; margin-top: 1rem; }
  input[type=text], input[type=password] { width: 100%; padding: .55rem; margin: .3rem 0 .9rem;
         border: 1px solid #8886; border-radius: 8px; box-sizing: border-box; font-size: 1rem; }
  .code { letter-spacing: .4em; text-align: center; font-size: 1.4rem; }
  button { background: #2563eb; color: #fff; border: 0; border-radius: 8px;
           padding: .6rem 1.1rem; font-size: 1rem; cursor: pointer; }
  button.secondary { background: #6b7280; }
  .err { color: #dc2626; font-weight: 600; }
  .muted { color: #6b7280; font-size: .9rem; }
  .secret { font-family: ui-monospace, monospace; background: #8881; padding: .2rem .4rem;
            border-radius: 6px; word-break: break-all; }
  .qr { background: #fff; padding: 12px; border-radius: 8px; width: max-content; margin: .6rem auto; }
  .qr img { display: block; max-width: 100%; height: auto; }  /* never overflow/clip */
  nav { font-size: .85rem; color: #6b7280; margin-bottom: 1rem; }
  ol { padding-left: 1.2rem; }
</style>
<nav>SledgeHammer Airflow &middot; two-factor login demo
  {% if mode_ldap %}&middot; <b>real EECS LDAP</b>{% else %}&middot; password check stubbed{% endif %}
</nav>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{{ body|safe }}
"""


def _page(body_html: str, error: str = "", **ctx):
    return render_template_string(
        _BASE, body=render_template_string(body_html, **ctx),
        error=error, mode_ldap=(os.environ.get("SLEDGE_2FA_DEMO_LDAP") == "1"),
    )


_LOGIN = """
<h1>Sign in</h1>
<div class="card">
  <form method="post" action="{{ url_for('login') }}">
    <label>Username{% if not mode_ldap %} (any name){% endif %}</label>
    <input type="text" name="username" autofocus autocomplete="username" value="{{ username }}">
    <label>Password{% if not mode_ldap %} (any non-empty){% endif %}</label>
    <input type="password" name="password" autocomplete="current-password">
    <button type="submit">Continue</button>
  </form>
</div>
<p class="muted">After the password you'll be asked for a six-digit code from an
authenticator app. First time through, you'll scan a QR to set that up.</p>
"""

_ENROLL = """
<h1>Set up two-factor</h1>
<div class="card">
  <p>Scan this with Google Authenticator, Authy, 1Password, or any TOTP app:</p>
  {% if qr %}<div class="qr">{{ qr|safe }}</div>
  <p class="muted"><a href="{{ url_for('qr_png') }}" target="_blank">Open the QR as a full image</a>
     (scan that if the one above won't read)</p>{% else %}
  <p class="muted">(No QR backend installed -- enter the key by hand.)</p>{% endif %}
  <p class="muted">Can't scan? Add an account manually with this key:</p>
  <p><span class="secret">{{ secret }}</span></p>
  <form method="post" action="{{ url_for('enroll') }}">
    <label>Enter the 6-digit code the app now shows, to confirm:</label>
    <input class="code" type="text" name="code" inputmode="numeric" autocomplete="one-time-code"
           maxlength="6" autofocus>
    <button type="submit">Confirm &amp; finish</button>
  </form>
</div>
<p class="muted">Signing in as <b>{{ uid }}</b>. <a href="{{ url_for('logout') }}">Cancel</a></p>
"""

_VERIFY = """
<h1>Two-factor code</h1>
<div class="card">
  <form method="post" action="{{ url_for('verify') }}">
    <label>Enter the 6-digit code for <b>{{ uid }}</b>:</label>
    <input class="code" type="text" name="code" inputmode="numeric" autocomplete="one-time-code"
           maxlength="6" autofocus>
    <button type="submit">Sign in</button>
  </form>
</div>
<p class="muted"><a href="{{ url_for('logout') }}">Use a different account</a></p>
"""

_HOME = """
<h1>You're in &check;</h1>
<div class="card">
  <p>Signed in as <b>{{ uid }}</b>, with both factors verified.</p>
  <p class="muted">In the real deployment this is where Airflow's UI would load.</p>
  <form method="post" action="{{ url_for('reset') }}" style="margin-top:1rem">
    <button class="secondary" type="submit">Reset my 2FA (re-enroll next time)</button>
  </form>
</div>
<p class="muted"><a href="{{ url_for('logout') }}">Log out</a></p>
"""


# ---- routes ---------------------------------------------------------------

@app.route("/")
def index():
    if session.get("uid"):
        return redirect(url_for("home"))
    return _page(_LOGIN, username="")


@app.route("/login", methods=["POST"])
def login():
    uid = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not _password_ok(uid, password):
        return _page(_LOGIN, error="Wrong username or password.", username=uid)
    session.clear()
    session["pending_uid"] = uid.lower()
    if _store.is_enrolled(uid):
        return redirect(url_for("verify"))
    secret, uri = service.begin_enrollment(_store, uid)
    return redirect(url_for("enroll"))


@app.route("/enroll", methods=["GET", "POST"])
def enroll():
    uid = session.get("pending_uid")
    if not uid:
        return redirect(url_for("index"))
    enr = _store.get(uid)
    if not enr:
        # No pending secret (e.g. server restarted) -- start enrollment fresh.
        service.begin_enrollment(_store, uid)
        enr = _store.get(uid)
    if request.method == "POST":
        code = request.form.get("code") or ""
        if service.confirm_enrollment(_store, uid, code):
            session.clear()
            session["uid"] = uid
            return redirect(url_for("home"))
        return _page(_ENROLL, error="That code didn't match. Try the current one.",
                     uid=uid, secret=enr.secret,
                     qr=qr_img(_uri_for(uid, enr.secret)))
    return _page(_ENROLL, uid=uid, secret=enr.secret, qr=qr_img(_uri_for(uid, enr.secret)))


@app.route("/verify", methods=["GET", "POST"])
def verify():
    uid = session.get("pending_uid")
    if not uid:
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        locked = service.lock_remaining(_store, uid)
        if locked > 0:
            error = f"Too many incorrect codes. Try again in {locked}s."
        else:
            code = request.form.get("code") or ""
            if service.check_code(_store, uid, code):
                session.clear()
                session["uid"] = uid
                return redirect(url_for("home"))
            locked = service.lock_remaining(_store, uid)
            error = (f"Too many incorrect codes. Try again in {locked}s."
                     if locked > 0 else "Wrong or already-used code.")
    return _page(_VERIFY, error=error, uid=uid)


@app.route("/qr.png")
def qr_png():
    """The enrollment QR as a standalone PNG, with no page layout around it.
    Open it directly (or in a new tab) for a clean, full-size scan target.
    """
    uid = session.get("pending_uid")
    if not uid:
        return redirect(url_for("index"))
    enr = _store.get(uid)
    if not enr:
        return redirect(url_for("enroll"))
    import io
    import segno
    buf = io.BytesIO()
    segno.make(_uri_for(uid, enr.secret), error="m").save(
        buf, kind="png", scale=8, border=4, dark="#000000", light="#ffffff")
    return Response(buf.getvalue(), mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/home")
def home():
    uid = session.get("uid")
    if not uid:
        return redirect(url_for("index"))
    return _page(_HOME, uid=uid)


@app.route("/reset", methods=["POST"])
def reset():
    uid = session.get("uid")
    if uid:
        _store.delete(uid)
    session.clear()
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


def _uri_for(uid: str, secret: str) -> str:
    return service.totp.provisioning_uri(secret, uid, issuer=service.ISSUER)


def main():
    ap = argparse.ArgumentParser(description="LDAP + TOTP 2FA demo")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    print(f"[2fa-demo] store: {getattr(_store, 'path', 'postgres')}")
    print(f"[2fa-demo] LDAP password check: {'ON' if os.environ.get('SLEDGE_2FA_DEMO_LDAP')=='1' else 'stubbed (any password)'}")
    print(f"[2fa-demo] open http://{args.host}:{args.port}/")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
