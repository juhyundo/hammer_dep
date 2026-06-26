#!/usr/bin/env python3
"""Run ONLY the Airflow api-server, with the TOTP second factor switched on, on a
test port -- so you can try the real LDAP + 2FA login without touching the
running deployment.

It loads the same GPG secrets the normal launcher uses, sets SLEDGE_2FA=1, and
starts just the api-server. No scheduler is started, so it does not race the
production scheduler on the shared metadata DB, and the login on the normal port
is unchanged. Stop it with Ctrl-C; nothing it does is permanent.

    cd <checkout>
    export PATH=$(pwd)/.venv/bin:$PATH
    ./scripts/airflow-2fa-testserver.py            # serves on :8082

Then open http://localhost:8082/auth/login (tunnel the port if your browser is
elsewhere) and sign in with your EECS username, password, and an authenticator
code. First login walks you through QR enrollment.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_secrets() -> None:
    """Decrypt the same secrets file the standalone launcher uses, into env."""
    enc = os.path.expanduser(os.environ.get(
        "SLEDGE_SECRETS_FILE",
        os.path.join(REPO, ".sledgehammer", "airflow-secrets.env.gpg")))
    if not os.path.exists(enc):
        if os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"):
            print(f"[2fa-test] {enc} not found; using secrets already in the environment.")
            return
        sys.exit(f"[2fa-test] ERROR: no secrets at {enc} and none in the environment.")
    if not subprocess.run(["which", "gpg"], capture_output=True).returncode == 0:
        sys.exit("[2fa-test] ERROR: gpg not on PATH; cannot decrypt secrets.")
    try:
        if sys.stdin.isatty():
            os.environ.setdefault("GPG_TTY", os.ttyname(sys.stdin.fileno()))
    except Exception:
        pass
    print(f"[2fa-test] decrypting {enc} (enter your GPG passphrase) ...")
    attempts = 3
    res = None
    for attempt in range(1, attempts + 1):
        res = subprocess.run(
            ["gpg", "--quiet", "--no-symkey-cache", "--decrypt", enc],
            capture_output=True)
        if res.returncode == 0:
            break
        if attempt < attempts:
            print(f"[2fa-test] that passphrase didn't work "
                  f"(attempt {attempt}/{attempts}) -- try again, or Ctrl-C to quit.")
        else:
            sys.stderr.write(res.stderr.decode("utf-8", "ignore"))
            sys.exit(f"[2fa-test] ERROR: could not decrypt secrets after {attempts} tries.")
    loaded = 0
    for raw in res.stdout.decode("utf-8", "ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ[key] = val
        loaded += 1
    if not loaded:
        sys.exit("[2fa-test] ERROR: secrets file decrypted to no KEY=VALUE lines.")
    print(f"[2fa-test] loaded {loaded} secret(s).")


def main() -> None:
    _load_secrets()
    os.environ["SLEDGE_2FA"] = "1"
    # This test must read THIS checkout's airflow.cfg + webserver_config.py.
    os.environ["AIRFLOW_HOME"] = REPO
    os.environ.setdefault("AIRFLOW__CORE__DAGS_FOLDER", os.path.join(REPO, "dags"))
    port = os.environ.get("SLEDGE_2FA_TEST_PORT", "8082")
    print(f"[2fa-test] AIRFLOW_HOME={REPO}")
    print(f"[2fa-test] starting api-server with 2FA ON -> http://0.0.0.0:{port}/auth/login")
    print("[2fa-test] no scheduler started; the production stack and normal login are untouched.")
    os.execvp("airflow", ["airflow", "api-server", "-H", "0.0.0.0", "-p", port])


if __name__ == "__main__":
    main()
