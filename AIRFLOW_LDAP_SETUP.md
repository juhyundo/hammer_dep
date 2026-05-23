# Airflow LDAP login

Optional setup for replacing Airflow's default "shared admin password" login with proper per-user LDAP authentication. After this, each person logs into the Airflow webserver with their own institutional credentials (at Berkeley EECS, that's their EECS account). No shared accounts, separate sessions per user.

Skip this if you're only using the cache CLI (`hammer-pd-store`) and don't run the Airflow webserver. The cache layer doesn't care about LDAP.

## What you need before starting

- An LDAP server you can reach. Berkeley EECS uses `ldaps://ldap.eecs.berkeley.edu`; substitute your institution's URL if you're elsewhere.
- The Airflow stack already running on your machine (see DATABASE_SETUP.md if you haven't set that up yet).
- Permission to edit `airflow.cfg` and create `webserver_config.py` in `$AIRFLOW_HOME`.

## 1. Install FAB and python-ldap

Airflow 3 ships with `SimpleAuthManager` as the default. To use LDAP you need the FAB provider package plus a Python LDAP client. The Python LDAP client is a C extension, so it also needs OpenLDAP development headers (`lber.h` and friends) at build time. On machines without root, `conda install -n base -c conda-forge openldap` puts them where the build can find them.

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

The `jwt_issuer` setting is mandatory. Without it, login succeeds against LDAP but the post-login JWT signing step fails with `TypeError: Issuer (iss) must be a string`. The value just has to be a non-empty string; a project identifier is fine.

## 3. Drop in `webserver_config.py`

This file lives at the root of `$AIRFLOW_HOME`. It tells FAB how to talk to LDAP. The version checked into this repo is configured for EECS LDAP. For another institution, swap the `AUTH_LDAP_SERVER` and `AUTH_LDAP_SEARCH` values.

The non-obvious bit: EECS LDAP keys each user record by `eecsDWRosterID=<number>`, not by username. You can't construct a bind DN from "lawrencejrhee" directly. FAB has to do "search-and-bind" instead: connect anonymously, look up the user's actual DN by uid, then bind as that DN with the user's password.

FAB's documented way to enable search-and-bind is `AUTH_LDAP_BIND_USER` plus `AUTH_LDAP_BIND_PASSWORD` (a service account). EECS doesn't hand out service accounts, but it does allow anonymous search. The included config monkey-patches FAB's `_ldap_bind_indirect` to skip the service account bind and go straight to anonymous. Without the patch, FAB refuses to enter the search-bind flow at all.

To point at a different LDAP server, edit:

```python
AUTH_LDAP_SERVER = "ldaps://your-ldap-host"
AUTH_LDAP_SEARCH = "dc=your,dc=domain"
AUTH_LDAP_UID_FIELD = "uid"   # whatever attribute your usernames are stored under
```

If your LDAP server is keyed by uid normally (i.e. user DNs look like `uid=alice,ou=people,dc=...`), you don't need the monkey patch. Remove the patch block and drop `AUTH_LDAP_BIND_USER` / `AUTH_LDAP_BIND_PASSWORD`. FAB's default flow handles that case.

## 4. Don't use `airflow standalone` — use the included replacement

`airflow standalone` hardcodes SimpleAuthManager and overrides whatever you set in `airflow.cfg`. The relevant lines in `airflow/cli/commands/standalone_command.py`:

```python
# Make sure we're using SimpleAuthManager
if conf.get("core", "auth_manager") != simple_auth_manager_classpath:
    env["AIRFLOW__CORE__AUTH_MANAGER"] = simple_auth_manager_classpath
```

It also tries to look up SimpleAuthManager-specific user info, which crashes when `auth_manager` is FabAuthManager. So `airflow standalone` is unusable with LDAP.

The repo includes a drop-in replacement at `scripts/airflow-standalone-ldap.py`. It subclasses `StandaloneCommand`, removes the auth_manager override, and skips the SimpleAuthManager user-info step. Everything else (subprocess management, colored output for each component, Ctrl+C cleanup) is inherited unchanged. Run it the same way you'd run `airflow standalone`:

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

You'll have to manage their lifecycle yourself; Ctrl+C only kills one. The wrapper script is easier.

## 5. Test the login

Point a browser at `http://localhost:8081/auth/login` (with an SSH tunnel if your browser isn't on the same host as the api-server):

- Username: your institutional UID (e.g. `lawrencejrhee` for EECS — just the uid, no domain)
- Password: your institutional password

On success the UI loads with your real name in the top-right corner, and a new row appears in the `ab_user` table of the Airflow metadata DB, populated from your LDAP attributes (first name, last name, email).

## What the FAB auth flow actually does

For reference if you're debugging:

1. FAB sees `AUTH_LDAP_BIND_USER` is set → enters "Indirect Search Bind" flow.
2. Calls patched `_ldap_bind_indirect` → does `con.simple_bind_s()` (anonymous).
3. Runs `_search_ldap` with filter `(uid=lawrencejrhee)` → finds the DN.
4. Calls `_ldap_bind(user_dn, password)` → tries to bind as that DN. Succeeds iff the password is right.
5. On first successful login, FAB creates an `ab_user` row from the LDAP attributes (uid, first/last/email) with the `AUTH_USER_REGISTRATION_ROLE` role (default `User`).
6. Airflow signs a JWT with `iss=airflow-sledgehammer` (or whatever you set), sets a session cookie, redirects to the UI.

## Promoting a user to admin

By default, new users get the `User` role: view DAGs, trigger runs, but can't manage connections, variables, or other users.

To promote someone to `Admin`, run this against the Airflow metadata DB:

```sql
INSERT INTO ab_user_role (user_id, role_id)
SELECT (SELECT id FROM ab_user WHERE username='lawrencejrhee'),
       (SELECT id FROM ab_role WHERE name='Admin');
```

**Do not pass `id` manually.** Let the column take its default from the sequence. Manual `INSERT ... (id, ...) VALUES (MAX(id)+1, ...)` leaves the sequence behind the table, and the next LDAP user to log in for the first time will crash with `duplicate key value violates unique constraint "ab_user_role_pkey"` in the api-server log, because FAB's first-login provisioning uses `nextval()` and collides with your manual row.

The Airflow 2.x CLI shortcut for this was `airflow users add-role -u <user> -r Admin`. That subcommand was removed in Airflow 3.x. The UI under `Security → List Users` still works if you already have admin access.

## Per-user build directory isolation

The shared Airflow stack runs as one LocalExecutor under one OS user (whoever started `airflow-standalone-ldap.py`). Without extra protection, every task triggered through the UI runs in that user's cwd and touches the same `e2e/build-...` directory, regardless of which LDAP user clicked the button. The practical consequence is that a `clean` triggered by user B wipes user A's working build directory.

The isolation works through a `hammer_poc.user_workspaces` table keyed by LDAP username. Every `AIRFlow(context=context)` instantiation in the DAGs resolves the triggering user's workspace and pins `OBJ_DIR` to `<workspace_root>/<design>` before any tool runs. After that, `clean` only ever wipes the triggering user's directory; `syn`, `par`, and friends only ever read and write that directory. The Postgres `pd_blobs` cache is the one thing that intentionally crosses user boundaries — user B's `syn` can restore from a tarball user A produced, which is the whole point of the project.

### Registering a user's workspace

Defaults are auto-created on first login. To see what's registered:

```bash
hammer-pd-store workspace-list
```

To set or change a user's workspace explicitly:

```bash
hammer-pd-store workspace-set <username> /path/to/their/workspace_root
```

The daemon user has to have write permission to that path. Auto-registration uses a per-user subdirectory under the daemon user's `hammer` checkout, e.g. `~/hammer/e2e/build-sky130-cm-<username>`. To remove a registration (next call for that user re-auto-registers a fresh default):

```bash
hammer-pd-store workspace-unset <username>
```

### Verifying the isolation

You can prove the isolation works without coordinating two LDAP users. There's a self-contained test script:

```bash
./scripts/test_per_user_workspace.sh
```

It registers two sandbox usernames pointed at `/tmp/sledgehammer_test/*`, seeds marker files in each, then drives `AIRFlow_synpar.clean()` programmatically with a fabricated DAG run context (the same code path the real scheduler takes). It asserts that the triggering user's directory is wiped and the other user's directory is untouched, then cleans up after itself. Exit code 0 means isolation is working.

Two passes like this should appear:

```
=== Test 1: __test_bob triggers clean ===
[user-workspace] triggering_user='__test_bob' -> OBJ_DIR=/tmp/sledgehammer_test/__test_bob/gcd
  OK: /tmp/sledgehammer_test/__test_alice/gcd/marker.txt still present
  OK: /tmp/sledgehammer_test/__test_bob/gcd/marker.txt is gone
```

Run it after any change to `_resolve_workspace_obj_dir`, `_lookup_triggering_user_from_db`, or any `AIRFlow*.__init__`. It would have caught the SDK-proxy bug that briefly let one user's clean wipe another user's dir even after the isolation was in place.

### What this does and doesn't isolate

| Concern | Behavior |
|---|---|
| `clean` wipes only the triggering user's dir | Yes (the whole point) |
| `syn`/`par` write only to the triggering user's dir | Yes |
| File ownership on the filesystem | Still the daemon user — `LocalExecutor` runs all tasks as one OS user |
| Cache (`pd_blobs`) | Shared across users, by design |
| Triggering-user shown in run history | Yes (Airflow's `dag_run.triggering_user_name`) |
| Two users triggering the same DAG at once | Safe — different `OBJ_DIR`s, no filesystem race |

If you need OS-level file ownership (each user's build files owned by them, not by the daemon), the shared stack isn't enough — that requires either per-user Airflow deployments (each user runs their own `airflow-standalone-ldap.py` on a different port) or a CeleryExecutor with per-user workers. The shared stack is meant for demos and shared cache benefits; serious per-user usage is the per-stack model.

## Troubleshooting

**Login page hangs forever.** The api-server isn't running, or your browser's `localhost` resolves to a different machine. From a remote machine, `ssh -L 8081:localhost:8081 user@server` and load `http://localhost:8081/`.

**Login form returns "Invalid credentials" right away.** Either the LDAP server is unreachable, or the bind DN FAB constructed is wrong. Check the api-server log for the actual LDAP error. The most useful clue is the `result:` code in any `ldap.LDAPError`:

- `result: 34` — Invalid DN syntax. FAB is binding with something that's not a valid DN. Usually means the patched `_ldap_bind_indirect` isn't installed, or `AUTH_LDAP_USERNAME_FORMAT` would help.
- `result: 49` — Invalid credentials. Search worked, bind failed. Wrong password, or the LDAP server is rejecting the bind for some other reason.
- `result: 32` — No such object. Search filter didn't match. Check `AUTH_LDAP_UID_FIELD` matches what your LDAP server uses.

**Login succeeds but lands on "Something bad has happened. TypeError: Issuer (iss) must be a string."** `jwt_issuer` isn't set in `[api_auth]`. See step 2.

**`ab_user` table doesn't exist.** Run `airflow db migrate` to create the FAB tables.

**Login succeeds for one user but the second user gets a 500 with `duplicate key value violates unique constraint "ab_user_role_pkey"`.** Someone promoted a user to admin by passing `id` manually instead of letting the sequence assign it. See "Promoting a user to admin" above. Fix is to realign the sequence:

```sql
SELECT setval('ab_user_role_id_seq', COALESCE((SELECT MAX(id) FROM ab_user_role), 0) + 1, false);
```

Run the same for `ab_user_id_seq` and `ab_register_user_id_seq` if those drifted too.

**`hammer-shell-test` not found error when starting Airflow.** `.venv/bin` isn't first on PATH. Export it explicitly: `export PATH=$(pwd)/.venv/bin:$PATH`.

## Files in this repo for LDAP

- `webserver_config.py` — checked in. Configured for EECS LDAP. Edit for other institutions.
- `scripts/airflow-standalone-ldap.py` — checked in. The drop-in replacement for `airflow standalone` that respects your configured auth_manager.
- `scripts/test_per_user_workspace.sh` — checked in. Regression test for the per-user workspace isolation.
- `airflow.cfg` — gitignored. Each deployment maintains its own. The two settings to add are `auth_manager` and `jwt_issuer`, as described in step 2.
- This file (`AIRFLOW_LDAP_SETUP.md`) — what you're reading.
