"""Wire the TOTP second factor into Airflow's FAB login.

The stock LDAP login (flask_appbuilder.security.views.AuthLDAPView) verifies the
password and immediately calls login_user. This subclass splits that in two:

  1. /auth/login  verifies the EECS password as before, but instead of logging
                  the user in, it parks their id in the session and sends them
                  to the second-factor page.
  2. /auth/mfa    asks for the authenticator code (or walks a first-time user
                  through QR enrollment). Only after a good code does it call
                  login_user.

Nothing here runs unless install_2fa() swaps the view in, and install_2fa() only
does that when SLEDGE_2FA=1. With the flag off, the login behaves exactly as it
does today, so turning 2FA on is a deliberate, reversible step.

To enable it, add to webserver_config.py (after the LDAP block):

    from auth2fa.fab_integration import install_2fa
    install_2fa()

and start the server with SLEDGE_2FA=1.
"""
from __future__ import annotations

import os

from flask import flash, g, redirect, render_template_string, request, session, url_for
from flask_appbuilder._compat import as_unicode
from flask_appbuilder.security.decorators import no_cache
from flask_appbuilder.security.forms import LoginForm_db
from flask_appbuilder.security.views import AuthLDAPView
from flask_appbuilder.utils.base import get_safe_redirect
from flask_login import login_user
from flask_appbuilder.baseviews import expose

from . import service, totp
from .qr import qr_img

_store = None


def _csrf_token() -> str:
    """A CSRF token for the hand-rolled forms. Works whether or not CSRFProtect
    is globally enabled; generate_csrf also seeds the session so validation
    passes when it is.
    """
    try:
        from flask_wtf.csrf import generate_csrf
        return generate_csrf()
    except Exception:
        return ""


def _get_store():
    """The Postgres-backed store, created once. The table is ensured on first use."""
    global _store
    if _store is None:
        from .store import get_store
        _store = get_store("postgres")
    return _store


_STYLE = """
<style>
  :root { color-scheme: light; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 26rem;
         margin: 3rem auto; padding: 0 1rem; line-height: 1.5;
         background: #fff; color: #1a1a1a; }
  h1 { font-size: 1.25rem; }
  .card { border: 1px solid #8884; border-radius: 12px; padding: 1.4rem; margin-top: 1rem; }
  input[type=text] { width: 100%; padding: .55rem; margin: .3rem 0 .9rem; border: 1px solid #8886;
         border-radius: 8px; box-sizing: border-box; font-size: 1rem; letter-spacing: .35em;
         text-align: center; }
  button { background: #2563eb; color: #fff; border: 0; border-radius: 8px; padding: .6rem 1.1rem;
         font-size: 1rem; cursor: pointer; }
  .err { color: #dc2626; font-weight: 600; }
  .muted { color: #6b7280; font-size: .9rem; }
  .secret { font-family: ui-monospace, monospace; background: #8881; padding: .2rem .4rem;
         border-radius: 6px; word-break: break-all; }
  .qr { background: #fff; padding: 12px; border-radius: 8px; width: max-content; margin: .6rem auto; }
  .qr img { display: block; max-width: 100%; height: auto; }  /* never overflow/clip */
</style>
"""

_ENROLL = _STYLE + """
<h1>Set up two-factor authentication</h1>
<div class="card">
  <p>Signed in as <b>{{ uid }}</b>. Scan this with an authenticator app
     (Google Authenticator, Authy, 1Password, Microsoft Authenticator):</p>
  {% if qr %}<div class="qr">{{ qr|safe }}</div>{% endif %}
  <p class="muted">Or add it by hand with this key:</p>
  <p><span class="secret">{{ secret }}</span></p>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf }}">
    <label>Enter the 6-digit code the app shows, to confirm:</label>
    <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code"
           maxlength="6" autofocus>
    <button type="submit">Confirm &amp; sign in</button>
  </form>
</div>
"""

_VERIFY = _STYLE + """
<h1>Two-factor code</h1>
<div class="card">
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf }}">
    <label>Enter the 6-digit code for <b>{{ uid }}</b>:</label>
    <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code"
           maxlength="6" autofocus>
    <button type="submit">Sign in</button>
  </form>
</div>
"""


class TwoFactorAuthLDAPView(AuthLDAPView):
    """LDAP login with a TOTP second factor bolted on."""

    @expose("/login/", methods=["GET", "POST"])
    @no_cache
    def login(self):
        if g.user is not None and g.user.is_authenticated:
            return redirect(self.appbuilder.get_url_for_index)
        form = LoginForm_db()
        if form.validate_on_submit():
            next_url = get_safe_redirect(request.args.get("next", ""))
            user = self.appbuilder.sm.auth_user_ldap(form.username.data, form.password.data)
            if not user:
                flash(as_unicode(self.invalid_login_message), "warning")
                return redirect(self.appbuilder.get_url_for_login_with(next_url))
            # Password is right, but they're not in yet. Hold the user id and
            # send them to the second factor. login_user happens only in mfa().
            session["mfa_pending_user_id"] = user.id
            session["mfa_next_url"] = next_url
            return redirect(url_for(f"{self.endpoint}.mfa"))
        return self.render_template(
            self.login_template, title=self.title, form=form, appbuilder=self.appbuilder
        )

    @expose("/logout/")
    def logout(self):
        # Drop any half-finished second-factor state too, so logging out never
        # leaves a pending user id parked in the session.
        self._clear_pending()
        return super().logout()

    @expose("/mfa/", methods=["GET", "POST"])
    @no_cache
    def mfa(self):
        user_id = session.get("mfa_pending_user_id")
        if not user_id:
            return redirect(self.appbuilder.get_url_for_login)
        user = self.appbuilder.sm.get_user_by_id(user_id)
        if not user:
            self._clear_pending()
            return redirect(self.appbuilder.get_url_for_login)
        uid = user.username
        store = _get_store()
        enrolled = store.is_enrolled(uid)

        error = ""
        if request.method == "POST":
            locked = service.lock_remaining(store, uid)
            if locked > 0:
                error = f"Too many incorrect codes. Try again in {locked}s."
            else:
                code = request.form.get("code") or ""
                ok = (service.confirm_enrollment(store, uid, code) if not enrolled
                      else service.check_code(store, uid, code))
                if ok:
                    return self._complete_login(user)
                # check_code may have just tripped the lockout on this failure.
                locked = service.lock_remaining(store, uid)
                error = (f"Too many incorrect codes. Try again in {locked}s."
                         if locked > 0 else "That code didn't match.")

        return self._render_factor(store, uid, enrolled, error=error)

    def _render_factor(self, store, uid, enrolled, error=""):
        csrf = _csrf_token()
        if enrolled:
            return render_template_string(_VERIFY, uid=uid, error=error, csrf=csrf)
        enr = store.get(uid)
        if not enr:
            service.begin_enrollment(store, uid)
            enr = store.get(uid)
        uri = totp.provisioning_uri(enr.secret, uid, issuer=service.ISSUER)
        return render_template_string(_ENROLL, uid=uid, secret=enr.secret,
                                      qr=qr_img(uri), error=error, csrf=csrf)

    def _complete_login(self, user):
        next_url = session.get("mfa_next_url") or self.appbuilder.get_url_for_index
        self._clear_pending()
        login_user(user, remember=False)
        return redirect(next_url)

    @staticmethod
    def _clear_pending():
        for k in ("mfa_pending_user_id", "mfa_next_url"):
            session.pop(k, None)


def install_2fa(force: bool = False) -> bool:
    """Swap the LDAP login view for the 2FA one. Returns True if it took effect.

    Call this from webserver_config.py. It's a no-op unless SLEDGE_2FA=1 (or
    force=True), so the flag is the single switch that turns 2FA on or off.

    Must run before FAB builds the auth view (webserver_config.py is imported at
    that point), same as the auth_user_ldap patch already in that file.
    """
    if not force and os.environ.get("SLEDGE_2FA") != "1":
        return False
    from airflow.providers.fab.auth_manager.security_manager.override import (
        FabAirflowSecurityManagerOverride,
    )
    FabAirflowSecurityManagerOverride.authldapview = TwoFactorAuthLDAPView
    return True
