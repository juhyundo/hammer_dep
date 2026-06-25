"""Custom two-factor authentication for the SledgeHammer Airflow login.

The first factor is the existing EECS LDAP password check (webserver_config.py).
This package adds a second factor: a TOTP code from the user's authenticator app.

Pieces:
  totp.py            the RFC 6238 code generation and verification (stdlib only)
  store.py           where per-user secrets live (SQLite for the demo, Postgres
                     for the real deployment)
  qr.py              renders the enrollment QR code
  demo_app.py        a standalone, runnable demo of the whole flow
  fab_integration.py wires the second factor into Airflow's FAB login view

Run `python -m auth2fa.demo_app` to try the flow in a browser without touching
the live Airflow deployment.
"""
