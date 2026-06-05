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

# Standalone uses these
from airflow.cli.commands.standalone_command import StandaloneCommand
from airflow.executors.executor_loader import ExecutorLoader
from airflow.executors import executor_constants


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
