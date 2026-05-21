# Setting up SledgeHammer Studio on your own Postgres

Quick guide for getting the cache and permissions running against any Postgres cluster. About 15 minutes if Postgres is already installed.

## What you need

- A Postgres cluster you control (laptop, lab server, RDS, conda Postgres, whatever). You need to be able to `CREATE DATABASE` and `CREATE ROLE` on it.
- This repo, on the `sledgehammer_merged` branch.
- A working venv (we use `.venv/` at the repo root).

## Setup

### 1. Clone and activate the venv

```bash
git clone https://github.com/lawrencejrhee/hammer.git
cd hammer
git checkout sledgehammer_merged
source ./venv.sh
```

`venv.sh` activates `.venv` and sets `AIRFLOW_HOME` to the repo root. If `.venv/` doesn't exist yet, bootstrap it with whatever your team uses (`uv sync`, `pip install -e .`, etc.) before sourcing.

### 2. Point at your Postgres

The library reads connection settings from env vars first, then from `airflow.cfg`'s `sql_alchemy_conn` if env vars aren't set. For a fresh setup, env vars are easiest:

```bash
export HAMMER_PG_HOST=your-postgres-host
export HAMMER_PG_PORT=5432
export HAMMER_PG_USER=your-username
export HAMMER_PG_PASSWORD=your-password
export HAMMER_PG_DB=sledgehammer_studio
```

If you're on barney specifically, copy or write a local `airflow.cfg` with the right `sql_alchemy_conn`. It's gitignored, so it never gets committed.

### 3. Create the cache database

Connect as a superuser (or anyone with `CREATEDB`) and run:

```sql
CREATE DATABASE sledgehammer_studio OWNER your_username;
```

If you're using your own cluster you're already a superuser, so this works without help. On a shared cluster like barney, your account just needs `CREATEDB` (most do).

### 4. Create the group role

This is the cluster-level role that gates cache access. One-time setup at the cluster level by anyone with `CREATEROLE` or superuser:

```sql
CREATE ROLE sledgehammer_users NOLOGIN;
GRANT sledgehammer_users TO your_username WITH ADMIN OPTION;
```

The `WITH ADMIN OPTION` part is what lets you add and remove members later without going back to the cluster admin.

### 5. Initialize the schema

```bash
hammer-pd-store init
```

This creates the `hammer_poc` schema, three tables (`master_databases`, `pd_blobs`, `pd_artifacts`), indexes, and applies default-deny on the schema with grants to `sledgehammer_users`. Idempotent, so re-running it later is fine.

### 6. Verify

```bash
hammer-pd-store blob-list
```

Prints `(no blobs)` on a fresh install. If you see a connection error, the env vars or password are off.

## Onboarding teammates

For each person who should have cache access:

```bash
hammer-pd-store grant their_postgres_username
```

That adds them to `sledgehammer_users`. They can now read and write the cache.

To remove someone:

```bash
hammer-pd-store revoke their_postgres_username
```

Anyone outside the group can't connect to the cache database at all. Postgres rejects them at the `CONNECT` check before any row-level logic runs.

## Running with the cache

The cache is opt-in. Turn it on with `HAMMER_PD_CACHE=1`:

```bash
export HAMMER_PD_CACHE=1
hammer-vlsi syn ...
```

First run, you'll see in the logs:

```
PD cache MISS for synthesis (sha256=...). Running stage.
PD cache STORE synthesis (sha256=..., bytes=...).
```

Subsequent runs with the same inputs:

```
PD cache HIT for synthesis (sha256=...). Restored syn-rundir, skipping run.
```

The second run completes in seconds instead of however long Genus takes.

## Testing the permission model

There's a runnable demo at `scripts/demo_auth.sh`. It spins up a throwaway local Postgres, creates `sledgehammer_users` plus three test users (`lawrencejrhee`, `colin`, `juhyun`), and walks through the full grant/revoke flow with pass/fail output. Useful sanity check any time you want to confirm the model still works:

```bash
./scripts/demo_auth.sh
```

Needs `initdb`, `pg_ctl`, `psql` on your `PATH`. Conda's `postgresql` package gives you all three (`conda install -n base postgresql`).

## Common gotchas

**`hammer-shell-test` not found when running flows.** Your `.venv/bin` isn't first on `PATH`. Source `venv.sh`, and if `which hammer-shell-test` still comes up empty, prepend `.venv/bin` to PATH manually. The BWRC env script can shove conda's bin in front of yours.

**Connection error from `hammer-pd-store`.** Either no Postgres password resolved (check `HAMMER_PG_PASSWORD` and `airflow.cfg`), or host/port/user is wrong. The error message usually says which.

**`permission denied for schema hammer_poc` when you try to read or write.** You aren't in `sledgehammer_users`. Have someone with admin on the role run `hammer-pd-store grant your_username`.

**`hammer-pd-store grant` fails with "must be admin of role".** You don't have `ADMIN OPTION` on `sledgehammer_users`. Cluster admin needs to grant it with `GRANT sledgehammer_users TO you WITH ADMIN OPTION`.

**`permission denied to create database`.** You don't have `CREATEDB`. Either someone grants it (`ALTER ROLE you CREATEDB`) or they create the database for you.

## What's on git, what isn't

The code on git includes the schema definition (the DDL lives inside `hammer/vlsi/pd_store.py`) and the CLI that applies it. When someone clones the repo and runs `hammer-pd-store init`, the DDL recreates the schema in whichever Postgres they point at.

What isn't on git: the actual data in the tables, the running Postgres server, passwords (`airflow.cfg` is gitignored). Each deployment runs its own database with its own state. The repo is the recipe; the database is whatever you build with it.

If you ever want to move data between deployments, that's a `pg_dump | pg_restore` thing, not a git thing.

## What comes after the basics

Once the cache is running, the usual next asks:

- LDAP authentication instead of Postgres passwords. Tie cache access to your institutional login. We've discussed three ways to do this; see `PD_STORE_README.md`.
- Per-project group roles. Today there's one `sledgehammer_users`. You could split into `project_gcd`, `project_rocket`, etc., with Row Level Security on the `owner` column for per-row visibility.
- A dedicated host account so the cache database isn't tied to any one person.

None of these block getting started. They're evolutions of the same model.
