"""sledgehammer: the branded launcher + CLI for the SledgeHammer Airflow stack.

Running ``sledgehammer`` with no arguments brings the full stack up with the
LDAP login and the TOTP second factor on -- it sets SLEDGE_2FA=1 and runs the
standalone launcher. Any other arguments pass straight through to the Airflow
CLI, with the database secrets and AIRFLOW_HOME loaded first, so commands like
``sledgehammer db migrate`` work without exporting anything by hand.

  sledgehammer                 launch the LDAP + 2FA stack (SLEDGE_2FA=1)
  sledgehammer standalone      same as above
  sledgehammer db migrate      run an airflow command with secrets loaded
  sledgehammer dags list       (any airflow subcommand works)
  SLEDGE_2FA=0 sledgehammer    launch without the second factor (plain LDAP)

Set SLEDGE_DRYRUN=1 to print what it would run instead of running it.
"""
import os
import subprocess
import sys

# hammer/shell/sledgehammer_cli.py -> repo root is three levels up.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCHER = os.path.join(REPO, "scripts", "airflow-standalone-ldap.py")
# First-arg values that mean "bring the server up" rather than an airflow passthrough.
LAUNCH_WORDS = {"", "standalone", "up", "start", "serve"}
HELP_WORDS = {"-h", "--help", "help"}


def _venv_bin(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), name)


def _load_secrets() -> None:
    """Decrypt the same GPG secrets the launcher uses into the environment, so
    airflow passthrough commands can reach the metadata DB. No-op if there's no
    secrets file (airflow then uses whatever is already in the environment).
    """
    enc = os.path.expanduser(os.environ.get(
        "SLEDGE_SECRETS_FILE", os.path.join(REPO, ".sledgehammer", "airflow-secrets.env.gpg")))
    if not os.path.exists(enc):
        return
    try:
        if sys.stdin.isatty():
            os.environ.setdefault("GPG_TTY", os.ttyname(sys.stdin.fileno()))
    except Exception:
        pass
    attempts = 3
    res = None
    for attempt in range(1, attempts + 1):
        res = subprocess.run(
            ["gpg", "--quiet", "--no-symkey-cache", "--decrypt", enc],
            capture_output=True)
        if res.returncode == 0:
            break
        if attempt < attempts:
            print(f"[sledgehammer] that passphrase didn't work "
                  f"(attempt {attempt}/{attempts}) -- try again, or Ctrl-C to quit.")
        else:
            sys.stderr.write(res.stderr.decode("utf-8", "ignore"))
            sys.exit(f"[sledgehammer] could not decrypt secrets after {attempts} tries.")
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


def main() -> int:
    # Pin AIRFLOW_HOME so the checkout's airflow.cfg + webserver_config.py are
    # read (not the ~/airflow defaults that would drop LDAP and use port 8080).
    os.environ["AIRFLOW_HOME"] = REPO
    args = sys.argv[1:]
    sub = args[0] if args else ""

    if sub in HELP_WORDS:
        sys.stdout.write(__doc__)
        return 0

    if sub in LAUNCH_WORDS:
        # Branded launch: LDAP + 2FA on by default; SLEDGE_2FA=0 opts out.
        os.environ.setdefault("SLEDGE_2FA", "1")
        cmd = [sys.executable, LAUNCHER] + args[1:]
        if os.environ.get("SLEDGE_DRYRUN"):
            print(f"[dryrun] launch  SLEDGE_2FA={os.environ['SLEDGE_2FA']} "
                  f"AIRFLOW_HOME={os.environ['AIRFLOW_HOME']}  ->  {' '.join(cmd)}")
            return 0
        os.execv(sys.executable, cmd)

    # Otherwise pass straight through to the airflow CLI, secrets loaded first.
    cmd = [_venv_bin("airflow")] + args
    if os.environ.get("SLEDGE_DRYRUN"):
        print(f"[dryrun] passthrough  AIRFLOW_HOME={os.environ['AIRFLOW_HOME']} "
              f"(secrets loaded)  ->  {' '.join(cmd)}")
        return 0
    _load_secrets()
    os.execv(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main())
