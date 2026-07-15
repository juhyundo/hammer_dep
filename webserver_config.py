"""Airflow webserver auth: EECS LDAP via FAB."""

import os

# python-ldap 3.x does NOT auto-import its submodules: a bare `import ldap`
# leaves `ldap.filter` / `ldap.dn` unset. Airflow's FAB LDAP login calls
# `ldap.filter.escape_filter_chars(...)` without importing it first, so a fresh
# login dies with "module 'ldap' has no attribute 'filter'". Importing the
# submodules here registers them in sys.modules globally (this config is loaded
# by the FAB app at startup, before any login), so the auth path finds them.
#
# This lives here rather than in a site-packages .pth shim on purpose: it's
# version-controlled, survives package reinstalls, and stays scoped to the web
# app -- a .pth would also import ldap into every DAG-task subprocess.
import ldap  # noqa: F401  (python-ldap)
import ldap.dn  # noqa: F401
import ldap.filter  # noqa: F401

from flask_appbuilder.security.manager import AUTH_LDAP

# Use LDAP for login.
AUTH_TYPE = AUTH_LDAP

# Search-and-bind mode against EECS LDAP. The actual DN of each user is keyed
# by eecsDWRosterID (a numeric we don't know in advance), so we cannot
# construct the bind DN from the username. We MUST use FAB's "Indirect Search
# Bind" flow:
#   1. Bind to LDAP (anonymously, since EECS allows it).
#   2. Search for uid=<input> to find the user's actual DN.
#   3. Re-bind as that DN with the user's password.
#
# FAB's Indirect Search Bind requires AUTH_LDAP_BIND_USER to be truthy, and
# then calls _ldap_bind_indirect to bind as that user. We don't have a service
# account, so we patch _ldap_bind_indirect to do anonymous bind regardless.
from airflow.providers.fab.auth_manager.security_manager.override import (
    FabAirflowSecurityManagerOverride,
)

def _anonymous_bind(self, ldap, con):
    """Override of FAB's _ldap_bind_indirect: bind anonymously."""
    con.simple_bind_s()

FabAirflowSecurityManagerOverride._ldap_bind_indirect = _anonymous_bind

# Any truthy string here triggers FAB's indirect-search flow.
# The actual value is ignored because _ldap_bind_indirect is patched above.
AUTH_LDAP_BIND_USER = "anonymous"
AUTH_LDAP_BIND_PASSWORD = ""

# Login whitelist (DB-backed). Only uids in hammer_poc.login_whitelist may log
# in; anyone else is rejected before the bind, so a valid EECS password isn't
# enough on its own. It's a Postgres table managed with `studio whitelist`, so
# nothing about who's allowed lives in git.
#
# AIRFLOW_ALLOWED_UIDS (comma-separated) is a fallback: those uids get in even
# if the whitelist table can't be reached.
import logging as _logging
import getpass as _getpass
_wl_log = _logging.getLogger("airflow.webserver_config")

# Whoever launched the server (the OS user) is always allowed -- they own it and
# shouldn't be able to lock themselves out -- as is anyone in AIRFLOW_ALLOWED_UIDS.
# Everyone else goes through the DB whitelist.
try:
    _owner_uid = {_getpass.getuser().strip().lower()}
except Exception:
    _owner_uid = set()
_BOOTSTRAP_UIDS = _owner_uid | {
    u.strip().lower()
    for u in os.environ.get("AIRFLOW_ALLOWED_UIDS", "").split(",")
    if u.strip()
}

def _uid_allowed(username):
    uid = (username or "").strip().lower()
    if not uid:
        return False
    if uid in _BOOTSTRAP_UIDS:
        return True
    try:
        from hammer.vlsi import pd_store
        return pd_store.is_whitelisted(uid)
    except Exception as e:
        _wl_log.warning("whitelist DB check failed for %r (%s) -- denying.", uid, e)
        return False

_orig_auth_user_ldap = FabAirflowSecurityManagerOverride.auth_user_ldap
def _whitelisted_auth_user_ldap(self, username, password, rotate_session_id=True):
    if not _uid_allowed(username):
        _wl_log.warning("LOGIN REJECTED (not whitelisted): %r", username)
        return None
    user = _orig_auth_user_ldap(self, username, password, rotate_session_id=rotate_session_id)
    # Whoever runs this instance administers this instance: LDAP registration
    # hands out the plain User role, so promote the OS owner to Admin on
    # login. Teammates logging into someone else's instance stay User.
    if user is not None and (username or "").strip().lower() in _owner_uid:
        try:
            _admin = self.find_role("Admin")
            if _admin is not None and _admin not in user.roles:
                user.roles.append(_admin)
                self.update_user(user)
                _wl_log.info("promoted instance owner %r to Admin", username)
        except Exception as _e:
            _wl_log.warning("owner Admin promotion failed for %r (%s)", username, _e)
    if user is not None:
        try:
            _names = {r.name for r in user.roles}
            _member = self.find_role("Member")
            if _member is not None and "Admin" not in _names and "Member" not in _names:
                user.roles.append(_member)
                self.update_user(user)
        except Exception as _e:
            _wl_log.warning("Member attach failed for %r (%s)", username, _e)
    return user
FabAirflowSecurityManagerOverride.auth_user_ldap = _whitelisted_auth_user_ldap

AUTH_LDAP_SERVER = "ldaps://ldap.eecs.berkeley.edu"
AUTH_LDAP_SEARCH = "dc=eecs,dc=berkeley,dc=edu"
AUTH_LDAP_UID_FIELD = "uid"
AUTH_LDAP_USE_TLS = False  # we're using ldaps:// directly, not STARTTLS

# When a new LDAP user logs in for the first time, register them automatically
# with the role below. Without this, they'd fail login because they have no
# Airflow user record yet.
AUTH_USER_REGISTRATION = True
AUTH_USER_REGISTRATION_ROLE = "User"   # 'Admin', 'User', 'Op', 'Viewer', 'Public'

# Map LDAP attributes to Airflow user fields. Optional but makes the UI
# show real names instead of just usernames.
AUTH_LDAP_FIRSTNAME_FIELD = "givenName"
AUTH_LDAP_LASTNAME_FIELD = "sn"
AUTH_LDAP_EMAIL_FIELD = "mail"

# Session security: each LDAP user gets their own session cookie. No leakage.
WTF_CSRF_ENABLED = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# If TLS verification ever gets in the way (self-signed certs, etc.), set
# this. Default is "demand" which is strict.
# AUTH_LDAP_TLS_DEMAND = False

# Optional TOTP second factor. install_2fa() swaps the LDAP login view for one
# that also asks for an authenticator code, but only when SLEDGE_2FA=1 -- with
# the flag unset it does nothing and login is exactly as configured above. The
# auth2fa package sits at the checkout root next to this file, which isn't always
# on sys.path when FAB loads this config, so add it. Any failure here must not
# break login, hence the guard.
import sys as _sys
_repo_root = os.path.dirname(os.path.abspath(__file__))
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)
try:
    from auth2fa.fab_integration import install_2fa
    if install_2fa():
        _wl_log.warning("two-factor (TOTP) second factor is ON")
except Exception as _e:
    _wl_log.warning("two-factor second factor not installed (%s); LDAP login unchanged", _e)
