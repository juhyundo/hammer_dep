#!/usr/bin/env python3
"""
Drop-in replacement for `airflow standalone` that respects the configured
auth_manager (e.g. FabAuthManager for LDAP) instead of forcing
SimpleAuthManager.

Upstream `airflow standalone` overrides AIRFLOW__CORE__AUTH_MANAGER on every
launch, regardless of what's in airflow.cfg, which makes LDAP login
impossible from the standalone command. This script reuses everything else
about StandaloneCommand (subprocess management, colored output, ready
detection, signal handling) and just skips the auth_manager override.

Usage:
    source ./venv.sh
    export PATH=$(pwd)/.venv/bin:$PATH
    ./scripts/airflow-standalone-ldap.py
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

# Airflow is imported further down, after _setup_secrets() runs -- see the note there.


def _port_in_use(port: int) -> bool:
    """True if something is already listening on 127.0.0.1:<port>."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_pgadmin(port: int) -> bool:
    """True if a pgAdmin server actually answers on 127.0.0.1:<port>."""
    if not _port_in_use(port):
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/browser/", timeout=3) as r:
            return "pgadmin" in r.read(8192).decode("utf-8", "ignore").lower()
    except Exception:
        return False


def _find_running_pgadmin(base_port: int, span: int = 12):
    """Find an already-running pgAdmin at/after base_port (it may have
    auto-incremented off a busy port). Returns the live port, or None."""
    for p in range(base_port, base_port + span):
        if _is_pgadmin(p):
            return p
    return None


def _read_pgadmin_port_from_log(log_path: str, timeout: int = 45):
    """Poll pgAdmin's startup output for the port it actually bound
    ('navigate to http://host:PORT'). Returns the real port, or None."""
    pat = re.compile(r"navigate to https?://[^:/\s]+:(\d+)", re.IGNORECASE)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(log_path, "r", errors="ignore") as f:
                hits = pat.findall(f.read())
            if hits:
                return int(hits[-1])
        except OSError:
            pass
        time.sleep(1)
    return None


# Single source of truth shared with the pgadmin_link plugin: the launcher
# writes the live pgAdmin URL here, the plugin reads it to build the nav link.
# Whatever port pgAdmin actually ends up on, the nav link follows -- no need to
# keep an env var in sync across the two.
PGADMIN_URL_FILE = os.path.expanduser("~/.pgadmin/airflow_pgadmin_url")


def _publish_pgadmin_url(port: int) -> None:
    """Record the live pgAdmin URL for the nav-link plugin to pick up."""
    try:
        os.makedirs(os.path.dirname(PGADMIN_URL_FILE), exist_ok=True)
        with open(PGADMIN_URL_FILE, "w") as f:
            f.write(f"http://localhost:{port}/browser/\n")
    except OSError:
        pass


def _start_pgadmin() -> None:
    """
    Launch pgAdmin alongside Airflow so the 'pgAdmin' nav-bar link works
    without a separate manual start.

    No-op when:
      * SLEDGE_NO_PGADMIN is set (opt out), or
      * something is already serving on the port (reuse it), or
      * pgadmin4 isn't installed.

    pgAdmin runs in its own session (survives a Ctrl-C meant for Airflow's
    subprocesses) and is terminated when this launcher exits normally. If the
    launcher is killed hard, pgAdmin is left running and simply reused next
    time (the port check makes startup idempotent).
    """
    if os.environ.get("SLEDGE_NO_PGADMIN"):
        return
    base_port = int(os.environ.get("PGADMIN_PORT", "5050"))
    pgadmin = shutil.which("pgadmin4") or os.path.expanduser("~/miniforge3/bin/pgadmin4")
    if not os.path.exists(pgadmin):
        print("[pgadmin] pgadmin4 not found (pip install pgadmin4); skipping auto-start.")
        return

    # 1. If a pgAdmin is already up, link the nav item to ITS actual port --
    #    even if a prior/manual start landed it on 5051 instead of 5050. This is
    #    what keeps the nav link and the real server in lock-step.
    running = _find_running_pgadmin(base_port)
    if running is not None:
        print(f"[pgadmin] found running pgAdmin on :{running}; linking the nav item to it.")
        _publish_pgadmin_url(running)
        return

    # 2. None running -> start one. Pin the base port only if it's free; if it's
    #    occupied, let pgAdmin auto-increment to the next free port. Either way
    #    we then read the ACTUAL port it reports and publish THAT, so the link
    #    can't drift from where pgAdmin really came up.
    log_dir = os.path.expanduser("~/.pgadmin")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "pgadmin4.startup.log")
    open(log_path, "w").close()  # clear so we read THIS run's 'navigate to' line
    logf = open(log_path, "ab")
    pg_env = dict(os.environ)
    if not _port_in_use(base_port):
        pg_env["PGADMIN_INT_PORT"] = str(base_port)
    proc = subprocess.Popen(
        [pgadmin],
        stdout=logf,
        stderr=logf,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=pg_env,
    )
    actual = _read_pgadmin_port_from_log(log_path) or base_port
    print(f"[pgadmin] started (pid {proc.pid}) -> http://127.0.0.1:{actual}/browser/  "
          f"(log: {log_path})")
    _publish_pgadmin_url(actual)
    # Intentionally NOT tied to this launcher's lifetime. pgAdmin keeps running
    # across Airflow restarts -- the probe at the top reuses it next time -- so a
    # quick standalone restart never leaves you with a dead pgAdmin + a broken
    # link, which is exactly what kept happening. Stop it by hand if you must:
    #   pkill -f pgadmin4   (or kill the pid printed above)


def _setup_secrets() -> None:
    """Decrypt the GPG secrets file and load its KEY=VALUE lines as env vars.

    airflow.cfg is committed with its secret fields blank; the real values live
    in ~/.config/sledgehammer/airflow-secrets.env.gpg and get injected here, in
    memory, so Airflow reads them instead of the blank cfg. Make the file with
    scripts/sledge-secrets-init.py.
    """
    enc = os.path.expanduser(
        os.environ.get("SLEDGE_SECRETS_FILE",
                       "~/.config/sledgehammer/airflow-secrets.env.gpg"))

    if not os.path.exists(enc):
        # Allow an already-populated environment (e.g. CI exported the vars).
        if os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"):
            print(f"[secrets] {enc} not found; using secrets already in the environment.")
            return
        sys.exit(
            f"[secrets] ERROR: no secrets file at {enc}, and no secrets in the "
            f"environment.\n"
            f"          airflow.cfg ships with blank secrets, so Airflow cannot "
            f"start without them.\n"
            f"          Create it once:  ./scripts/sledge-secrets-init.py\n"
            f"          (or point SLEDGE_SECRETS_FILE at an existing .gpg file).")

    if not shutil.which("gpg"):
        sys.exit("[secrets] ERROR: gpg not found on PATH; cannot decrypt secrets.")

    # pinentry needs to know the controlling terminal to prompt over SSH.
    try:
        if sys.stdin.isatty():
            os.environ.setdefault("GPG_TTY", os.ttyname(sys.stdin.fileno()))
    except Exception:
        pass

    print(f"[secrets] decrypting {enc} (enter your GPG passphrase) ...")
    res = subprocess.run(["gpg", "--quiet", "--decrypt", enc], capture_output=True)
    if res.returncode != 0:
        sys.stderr.write(res.stderr.decode("utf-8", "ignore"))
        sys.exit(f"[secrets] ERROR: could not decrypt {enc} "
                 f"(wrong passphrase or corrupt file). Aborting startup.")

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
        sys.exit(f"[secrets] ERROR: {enc} decrypted to no KEY=VALUE lines.")
    print(f"[secrets] loaded {loaded} secret(s) into the environment.")


# Load the secrets, THEN import Airflow: importing it reads sql_alchemy_conn right
# away, so the environment has to be populated first or it crashes on a blank conn.
_setup_secrets()

from airflow.cli.commands.standalone_command import StandaloneCommand
from airflow.executors.executor_loader import ExecutorLoader
from airflow.executors import executor_constants


class HammerStandalone(StandaloneCommand):
    """StandaloneCommand without the SimpleAuthManager-specific behaviour."""

    def calculate_env(self):
        env = dict(os.environ)

        # Keep the LocalExecutor override (standalone is single-machine by design).
        executor_class, _ = ExecutorLoader.import_default_executor_cls()
        if not executor_class.is_local:
            self.print_output("standalone", "Forcing executor to LocalExecutor")
            env["AIRFLOW__CORE__EXECUTOR"] = executor_constants.LOCAL_EXECUTOR

        # Deliberately DO NOT override AIRFLOW__CORE__AUTH_MANAGER.
        # Whatever's in airflow.cfg (FabAuthManager, SimpleAuthManager, ...) wins.
        self.print_output("standalone", "Respecting configured auth_manager: not forcing SimpleAuthManager")
        return env

    def find_user_info(self):
        """
        Upstream's find_user_info() tries to print the SimpleAuthManager's
        auto-generated admin password. FabAuthManager has no such file (users
        come from LDAP), so we just print a hint and return.
        """
        self.print_output(
            "standalone",
            "Auth manager is not SimpleAuthManager: skipping admin-password lookup. "
            "Log in via your configured auth backend (e.g. LDAP).",
        )


def main():
    _start_pgadmin()
    HammerStandalone().run()


if __name__ == "__main__":
    sys.exit(main())
