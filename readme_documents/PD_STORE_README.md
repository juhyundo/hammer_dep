# SledgeHammer Studio: Postgres-backed cache for Hammer PD runs

A storage layer for Hammer that caches per stage build directories in
Postgres, keyed by a content hash of the inputs that determine each stage's
output. When the same inputs come up again, on the same machine or a
different one, the stored tarball is restored instead of rerunning the
tool. The result is that identical PD work runs once, ever.

This work integrates with the dependency team's per stage fingerprinting
(`vlsi.rtl_fingerprint_sha256`, `<stage>.hooks_fingerprint_sha256`,
`stage_change_check`) so a cache hit on our side equals a "no rerun"
decision on theirs.

## What this gets you today

* Two real tables in Postgres on barney (`master_databases`, `pd_blobs`),
  plus the older par-input JSON store from the earlier POC
  (`pd_artifacts`).
* A `compute_stage_key` function that hashes the slice of a master_database
  that actually determines a stage's output. Matches the comparison surface
  of the dependency team's `stage_change_check`.
* A CLI (`hammer-pd-store`) covering manual push, pull, listing, and access
  management.
* An opt-in cache wrapper around `driver.run_synthesis` and `driver.run_par`
  in `cli_driver.py`. With `HAMMER_PD_CACHE=1`, syn or par will skip the
  tool and untar a stored result if one matches.
* Permission gating through a single Postgres group role
  (`sledgehammer_users`). Members can read and write the cache. Anyone
  outside the group can't connect to the tables.

## Tables

All three live in the `hammer_poc` schema inside `airflow_lawrence` on
`barney.eecs.berkeley.edu:5433`.

```sql
CREATE TABLE hammer_poc.pd_artifacts (
    sha256      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    top_module  TEXT,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE hammer_poc.master_databases (
    design      TEXT PRIMARY KEY,
    db          JSONB NOT NULL,
    owner       TEXT NOT NULL DEFAULT current_user,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE hammer_poc.pd_blobs (
    sha256      TEXT PRIMARY KEY,
    stage       TEXT NOT NULL,
    data        BYTEA NOT NULL,
    size_bytes  BIGINT NOT NULL,
    owner       TEXT NOT NULL DEFAULT current_user,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`pd_artifacts` was the original par-input JSON store. It still works and
hasn't been removed, but the real caching path is `master_databases` +
`pd_blobs`. The `owner` column on the two newer tables exists so that flipping
on Row Level Security later (for the per row "hash not found" semantics
Andre asked about) is a one line policy change.

## CLI

Connection settings come from `HAMMER_PG_*` environment variables, then
fall back to `sql_alchemy_conn` in `airflow.cfg`, then to defaults. Source
`./venv.sh` to set `AIRFLOW_HOME` and activate the venv before running
anything.

### Setup and access

```bash
hammer-pd-store init              # create schema, tables, indexes, default deny
hammer-pd-store grant <role>      # add a Postgres role to sledgehammer_users
hammer-pd-store revoke <role>     # remove them
```

`init` is idempotent. Run it any number of times; it creates what's missing
and leaves the rest alone. It also reapplies the table grants to
`sledgehammer_users` if the role has since been created (see the DBA
prerequisite below).

### Master_database

```bash
hammer-pd-store master-push <design> [--master path]
hammer-pd-store master-pull <design> [--out path]
```

The master_database is keyed by design name (e.g. `gcd`). Latest write
wins. `--master` defaults to `./master_database.json`.

### Per stage tarballs

```bash
hammer-pd-store stage-key   <stage> [--master path]            # debug helper
hammer-pd-store stage-push  <stage> --rundir <path> [--master path]
hammer-pd-store stage-pull  <stage> --rundir <path> [--master path] [--overwrite]
hammer-pd-store blob-list   [--stage <tag>] [-n N]
```

`<stage>` is the stage tag, e.g. `synthesis`, `par`, `drc`, `lvs`. The
cache key for a tarball is computed from the master_database slice
relevant to that stage, so `stage-pull` finds what `stage-push` stored as
long as the inputs match.

### Legacy par-input JSON store

```bash
hammer-pd-store list
hammer-pd-store get <sha256>
hammer-pd-store put <path> [--kind <label>]
```

These still exist for the original par-input round trip POC. New work
should use the master_database and stage commands above.

## Automatic cache integration

`cli_driver.py` now wraps `driver.run_synthesis` and `driver.run_par` in
`pd_cache.cache_or_run`. Off by default. Turn it on with:

```bash
export HAMMER_PD_CACHE=1
hammer-vlsi syn ...
hammer-vlsi par ...
```

On each run, the wrapper:

1. Reads the live config from `driver.database.get_database_json()`.
2. Adds an RTL content fingerprint (`compute_rtl_fingerprint` over
   `synthesis.inputs.input_files`) so changes to RTL bytes invalidate the
   cache even though our local cli_driver doesn't yet have the dependency
   team's full fingerprinting.
3. Computes the per stage cache key (`compute_stage_key`).
4. Looks up `pd_blobs`. On hit, untars into the rundir and reads
   `<stage>-output.json` to reconstruct the output dict. On miss, runs the
   tool, then tars the rundir and pushes it for next time.

Failures are non fatal. DB outage, missing files, broken tarballs all log
a warning and fall through to a normal stage run.

## How the cache key is built

`compute_stage_key(master_db, stage_tag)` hashes the slice of
master_database that `stage_change_check` would compare. Specifically:

* Keep keys starting with `<stage_tag>.` (e.g. `synthesis.*`).
* Drop keys starting with `<stage_tag>.outputs.` (those are outputs of the
  stage, not inputs).
* Keep "global" keys that don't start with any known stage tag (e.g.
  `vlsi.core.technology`, `cadence.cadence_home`, `synopsys.*`).
* Drop any key ending in `.needsToRerun` (run bookkeeping, not an input).

The full list of known stage tags is `KNOWN_STAGE_TAGS` in `pd_store.py`:
`synthesis, par, drc, lvs, sram_generator, sim, power, formal, timing, pcb`.

Verified against Juhyun's real gcd `master_database.json`:

* Same input gives the same hash across repeated calls (stability).
* Mutating `synthesis.inputs.input_files` changes syn's hash (sensitivity).
* The same mutation doesn't change par's hash (selectivity).

`scripts/verify_stage_key.py` reproduces this check on any
`master_database.json` you point it at.

## Permissions

The default is no access. `init` runs `REVOKE ALL ON SCHEMA hammer_poc
FROM PUBLIC` and revokes table grants too. Access flows through one group
role:

```sql
sledgehammer_users          -- NOLOGIN role that owns access
```

Members of this role inherit `USAGE` on the schema and `SELECT, INSERT` on
every table now or later (the DDL uses `ALTER DEFAULT PRIVILEGES` so new
tables added under `hammer_poc` automatically pick up the same grants).
Onboarding a user is one line:

```bash
hammer-pd-store grant colin
```

That runs `GRANT sledgehammer_users TO colin`. Offboarding is:

```bash
hammer-pd-store revoke colin
```

A user without group membership trying to read or write `pd_blobs` gets a
Postgres permission error before any row visibility logic runs. There is
no per row gating yet; that's a future RLS policy on the `owner` column.

## One time DBA prerequisite

`hammer-pd-store grant` and `revoke` only work if the group role exists
and the calling role has `ADMIN OPTION` on it. We can't do this ourselves
on barney because `lawrencejrhee` does not have `CREATEROLE`. A DBA needs
to run:

```sql
CREATE ROLE sledgehammer_users NOLOGIN;
GRANT sledgehammer_users TO lawrencejrhee WITH ADMIN OPTION;
```

After that, re run `hammer-pd-store init` so the conditional grant block
in the DDL picks up the now existing role and applies the table grants to
it.

## Connection settings

Resolved in order. First hit wins per field.

1. `HAMMER_PG_*` env variables: `HAMMER_PG_HOST`, `HAMMER_PG_PORT`,
   `HAMMER_PG_DB`, `HAMMER_PG_USER`, `HAMMER_PG_PASSWORD`.
2. `sql_alchemy_conn` parsed out of `airflow.cfg`. Looked up at
   `$AIRFLOW_HOME/airflow.cfg`, then `./airflow.cfg`, then
   `~/airflow/airflow.cfg`.
3. Hardcoded defaults: `barney.eecs.berkeley.edu:5433/airflow_lawrence`,
   user `$USER`, password required (will raise if nothing resolves).

`venv.sh` exports `AIRFLOW_HOME=$(pwd)`, so sourcing it from the Hammer
root is usually enough to make path 2 work.

## Files

| File | What it holds |
|---|---|
| `hammer/vlsi/pd_store.py` | Schema, library, cache key derivation, RTL fingerprint, tar helpers, access management |
| `hammer/vlsi/pd_cache.py` | `cache_or_run` wrapper used by `cli_driver.py` for automatic caching |
| `hammer/vlsi/cli_driver.py` | Synthesis and par actions wrap their run calls in `cache_or_run` |
| `hammer/shell/pd_store_cli.py` | The `hammer-pd-store` CLI |
| `scripts/verify_stage_key.py` | Standalone correctness check for `compute_stage_key` |
| `pyproject.toml` | Registers `hammer-pd-store` as a console script |

## What's verified

* `compute_stage_key` against the real gcd `master_database.json`:
  stable, sensitive to syn changes, selective (par hash unchanged when
  syn inputs change).
* `hammer-pd-store` parser builds and all subcommands wire up.
* `cli_driver.py` still imports with the cache wrapper in place.
* Live round trip of the original par-input JSON store against barney
  (from the earlier POC).

## What's not yet done

* Live `init` on barney with the new tables and permissions.
* End to end demo of the cache layer: `init`, push gcd's master_database,
  push `syn-rundir`, pull both into a clean directory, diff against the
  original.
* `HAMMER_PD_CACHE=1` exercise: run syn cold, run again warm, confirm
  second run is a cache hit. Tweak a config, confirm miss. Revert,
  confirm hit again.
* Cross user permissions test with Colin (push as `lawrencejrhee`, read as
  `colin` after `grant`, confirm denied after `revoke`).
* RLS for per row visibility (the `owner` column is already in place; just
  need to enable RLS and add a policy).

## Open coordination with the dependency team

* Master_database addressing for multi user. Today: keyed by design name,
  latest write wins. May want per (design, user) or per (design, branch)
  later.
* Path rewriting. The master_database and the build directories contain
  absolute paths. Until those are made relative on store and re expanded
  on load, restored runs only work for the same user on the same machine.
  Likely belongs on the dependency team side (relativize before
  `commit_master_database`), but needs confirmation.

## Deferred (Andre flagged for later)

* LDAP wiring in `pg_hba.conf` and per user login roles. DBA work.
* Per row visibility through Row Level Security on the `owner` column.
* Cache key that also covers tool versions and environment, not just
  config and RTL.
* Eviction or storage management once `pd_blobs` grows.
* Migration tooling, async driver, connection pooling.
