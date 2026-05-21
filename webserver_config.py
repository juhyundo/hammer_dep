"""Airflow webserver auth: EECS LDAP via FAB."""

import os
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
