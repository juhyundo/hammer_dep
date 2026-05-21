# Airflow LDAP login

Optional setup for replacing Airflow's default "shared admin password" login with proper per-user LDAP authentication. After this, each person logs into the Airflow webserver with their own institutional credentials (at Berkeley EECS, that's their EECS account). No shared accounts, separate sessions per user.

Skip this if you're only using the cache CLI (`hammer-pd-store`) and don't run the Airflow webserver. The cache layer doesn't care about LDAP.

## What you need before starting

- An LDAP server you can reach. We use `ldaps://ldap.eecs.berkeley.edu`; you'd substitute your institution's URL.
- The Airflow stack already running on your machine (see DATABASE_SETUP.md if you haven't set that up yet).
- Permission to edit `airflow.cfg` and create `webserver_config.py` in `$AIRFLOW_HOME`.

## 1. Install FAB and python-ldap

Airflow 3 ships with `SimpleAuthManager` as the default. To use LDAP you need the FAB provider package plus a Python LDAP client. Compile-time you also need OpenLDAP development headers (`lber.h` etc.); on machines without root, `conda install -n base -c conda-forge openldap` puts them where the build can find them.

```bash
# system or conda OpenLDAP headers (only needed once):
conda install -n base -c conda-forge openldap   # or `apt install libldap2-dev libsasl2-dev` if you have root

# Python packages:
source ./venv.sh
export PATH=$(pwd)/.venv/bin:$PATH
CFLAGS="-I$CONDA_PREFIX/include" LDFLAGS="-L$CONDA_PREFIX/lib" \
    uv pip install "apache-airflow-providers-fab" "python-ldap"
```

Verify:

```bash
python -c "import flask_appbuilder, ldap; print('OK')"
```

## 2. Switch Airflow's auth_manager

Two settings in `airflow.cfg`:

```ini
[core]
auth_manager = airflow.providers.fab.auth_manager.fab_auth_manager.FabAuthManager
```

```ini
[api_auth]
jwt_issuer = airflow-sledgehammer
```

The `jwt_issuer` setting is required. Without it, login succeeds against LDAP but the post-login JWT signing step fails with `TypeError: Issuer (iss) must be a string`. The value can be anything string-ish; we use a project identifier.

## 3. Drop in `webserver_config.py`

This file lives at the root of `$AIRFLOW_HOME`. It tells FAB how to talk to LDAP. The version checked into this repo is configured for EECS LDAP. For another institution, swap the `AUTH_LDAP_SERVER` and `AUTH_LDAP_SEARCH` values.

The non-obvious bit: EECS LDAP keys each user record by `eecsDWRosterID=<number>`, not by username. We can't construct a bind DN from "lawrencejrhee" directly. So FAB has to do "search-and-bind": connect anonymously, look up the user's actual DN by uid, then bind as that DN with the user's password.

FAB's documented way to enable this is `AUTH_LDAP_BIND_USER` plus `AUTH_LDAP_BIND_PASSWORD` (a service account). We don't have one, and EECS allows anonymous search anyway. The included config monkey-patches FAB's `_ldap_bind_indirect` to skip the service account and go straight to anonymous bind. Without the patch, FAB would refuse to enter the search-bind flow at all.

If you need to point at a different LDAP server, edit:

```python
AUTH_LDAP_SERVER = "ldaps://your-ldap-host"
AUTH_LDAP_SEARCH = "dc=your,dc=domain"
AUTH_LDAP_UID_FIELD = "uid"   # whatever attribute your usernames are stored under
```

If your LDAP server is keyed by uid normally (i.e. user DNs look like `uid=alice,ou=people,dc=...`), you don't need the monkey patch. You can simplify by removing the patch block and dropping `AUTH_LDAP_BIND_USER`/`AUTH_LDAP_BIND_PASSWORD`. FAB's default flow handles that case.

## 4. Don't use `airflow standalone` — use the included replacement

`airflow standalone` hardcodes SimpleAuthManager and overrides whatever you set in `airflow.cfg`. The relevant lines in `airflow/cli/commands/standalone_command.py`:

```python
# Make sure we're using SimpleAuthManager
if conf.get("core", "auth_manager") != simple_auth_manager_classpath:
    env["AIRFLOW__CORE__AUTH_MANAGER"] = simple_auth_manager_classpath
```

It also tries to look up SimpleAuthManager-specific user info, which fails for FabAuthManager. So `airflow standalone` is unusable with LDAP.

The repo includes a drop-in replacement: **`scripts/airflow-standalone-ldap.py`**. It subclasses `StandaloneCommand`, removes the auth_manager override, and skips the SimpleAuthManager user-info step. Everything else (subprocess management, colored output for each component, Ctrl+C cleanup) is reused unchanged. Run it the same way you'd run `airflow standalone`:

```bash
source ./venv.sh
export PATH=$(pwd)/.venv/bin:$PATH
unset AIRFLOW__CORE__AUTH_MANAGER     # in case it leaked into this shell earlier
./scripts/airflow-standalone-ldap.py
```

You should see the familiar four components come up:

```
standalone | Respecting configured auth_manager: not forcing SimpleAuthManager
standalone | Starting Airflow Standalone
api-server | Uvicorn running on http://0.0.0.0:8081
scheduler  | ...
dag-processor | ...
triggerer  | ...
```

Ctrl+C shuts everything down cleanly.

### Alternative: start components individually

If for any reason the wrapper script doesn't work, the equivalent manual setup is:

```bash
source ./venv.sh
export PATH=$(pwd)/.venv/bin:$PATH
unset AIRFLOW__CORE__AUTH_MANAGER

airflow api-server -H 0.0.0.0 -p 8081 &     # the web UI; this is where login happens
airflow scheduler &                           # schedules DAG runs
airflow dag-processor &                       # parses DAGs
airflow triggerer &                           # optional, for deferrable tasks
```

You'd then need to manage their lifecycle yourself (Ctrl+C only kills one). Easier to use the wrapper script.

## 5. Test the login

In a browser pointed at `http://localhost:8081/auth/login` (with an SSH tunnel if your browser isn't on the same host as the api-server):

- Username: your institutional UID (e.g. `lawrencejrhee` for EECS — just the uid, no domain)
- Password: your institutional password

On success: the UI loads with your real name in the top-right corner. A new row appears in the `ab_user` table of the Airflow metadata DB, populated from your LDAP attributes (first name, last name, email).

## What the FAB auth flow actually does

For reference if you're debugging:

1. FAB sees `AUTH_LDAP_BIND_USER` is set → enters "Indirect Search Bind" flow.
2. Calls patched `_ldap_bind_indirect` → does `con.simple_bind_s()` (anonymous).
3. Runs `_search_ldap` with filter `(uid=lawrencejrhee)` → finds the DN.
4. Calls `_ldap_bind(user_dn, password)` → tries to bind as that DN. Succeeds iff the password is right.
5. On first successful login, FAB creates an `ab_user` row from the LDAP attributes (uid, first/last/email) with the `AUTH_USER_REGISTRATION_ROLE` role (default `User`).
6. Airflow signs a JWT with `iss=airflow-sledgehammer` (or whatever you set), sets a session cookie, redirects to the UI.

## Promoting a user to admin

By default, new users get the `User` role (can view DAGs, trigger runs; can't manage connections/variables or other users). To promote yourself to `Admin`:

```bash
airflow users add-role -u lawrencejrhee -r Admin
```

Or in the UI under `Security → List Users` (if you already have admin access).

## Troubleshooting

**Login page hangs forever.** The api-server isn't running, or your browser's `localhost` resolves to a different machine. From a remote machine, `ssh -L 8081:localhost:8081 user@server` and load `http://localhost:8081/`.

**Login form returns "Invalid credentials" right away.** Either the LDAP server is unreachable, or the bind DN FAB constructed is wrong. Check the api-server log for the actual LDAP error. The most useful clue is the `result:` code in any `ldap.LDAPError`:
- `result: 34` — Invalid DN syntax. FAB is binding with something that's not a valid DN. Usually means the patched `_ldap_bind_indirect` isn't installed, or `AUTH_LDAP_USERNAME_FORMAT` would help.
- `result: 49` — Invalid credentials. Search worked, bind failed. Wrong password, or the LDAP server is rejecting the bind for some other reason.
- `result: 32` — No such object. Search filter didn't match. Check `AUTH_LDAP_UID_FIELD` matches what your LDAP server uses.

**Login succeeds but lands on "Something bad has happened. TypeError: Issuer (iss) must be a string."** You forgot to set `jwt_issuer` in `[api_auth]`.

**`ab_user` table doesn't exist.** Run `airflow db migrate` to create the FAB tables.

**`hammer-shell-test` not found error when starting Airflow.** Your `.venv/bin` isn't first on PATH. Export it explicitly: `export PATH=$(pwd)/.venv/bin:$PATH`.

## Files in this repo for LDAP

- `webserver_config.py` — checked in. Configured for EECS LDAP. Edit for other institutions.
- `airflow.cfg` — gitignored. Each deployment maintains its own. The two settings to add are `auth_manager` and `jwt_issuer`, as described above.
- This file (`AIRFLOW_LDAP_SETUP.md`) — what you're reading.
