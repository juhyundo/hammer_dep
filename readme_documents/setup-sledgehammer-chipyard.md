# Setting up SledgeHammer inside Chipyard (Sp26 Tapeout)

The SledgeHammer variant of the Sp26 Tapeout Chipyard setup. It mirrors the official
course instructions but swaps the two "install hammer into conda" steps for the uv-based
SledgeHammer flow. If you just want vanilla hammer, follow the course README instead. Use
this if you need the SledgeHammer Airflow/Postgres flow.

## Keep in mind

- Don't work under `/home`. Use your `/bwrcq` (or `/scratch`) area.
- Always use the compute servers, not the login servers.
- Source `env.sh` (and the venv, see below) before running anything, in every new shell.
- Clean up your VLSI runs regularly so the shared disk doesn't fill up.

## The one big difference: two Python environments

The course installs hammer and the tech plugin **into the conda env** with `pip install -e`.
SledgeHammer can't do that. It needs Airflow plus a source-built psycopg2, which live in a
uv venv. So you run **two separate Python environments that never merge**, layered at run time:

| | Owns | Python | Managed by |
|---|---|---|---|
| **Chipyard** `.conda-env` | EDA/JVM toolchain (gcc, openjdk, sbt, verilator, riscv-tools, firtool) plus `make` | **3.10.14** (pinned `<3.11`) | conda / `build-setup.sh` |
| **SledgeHammer** `vlsi/hammer/.venv` | `hammer-vlsi`, the tstech16c plugin, Airflow plus Postgres | **3.11.15** | uv / `uv_setup.sh` |

They install independently (the uv setup needs nothing from conda), but the VLSI `make`
flow needs both on PATH at once: conda for the tools, the venv so `python3` is hammer's.
This works because Chipyard's build-time Python is stdlib glue. Only `ee290-vlsi` needs
hammer, and it gets the venv.

## Setup

Assume you start from the repo root (`cd chipyard`) and conda is active (you see `(base)`).

### 1. Clone and fix permissions

```bash
git clone <chipyard-repo> chipyard
# group-own to tstech16c and make new files inherit the group
chown -R ${USER}:tstech16c chipyard
find chipyard -type d -exec chmod g+s {} +
cd chipyard
```

### 2. Build the conda environment and toolchain

Same as the course: lean conda, skip the FireSim/FireMarshal steps (6 through 9).

```bash
./build-setup.sh --use-lean-conda -s 6 -s 7 -s 8 -s 9   # takes a while; creates .conda-env (Python 3.10.14)
source ./env.sh
```

`build-setup.sh` aborts if `.conda-env` already exists, so delete it or add `-s 1` to skip
the conda step when re-running.

### 3. Install hammer the SledgeHammer way (replaces the course's `pip install -e hammer`)

Clone the **SledgeHammer fork** into `vlsi/hammer` (not `ucb-bar/hammer`), then build its
uv environment. Do **not** `pip install -e hammer` into conda.

```bash
cd vlsi
rm -rf hammer
git clone <sledgehammer-fork> hammer     # e.g. depteam = git@github.com:juhyundo/hammer_dep.git
cd hammer
./scripts/uv_setup.sh                     # run from the hammer repo root
cd ../..
```

`uv_setup.sh` installs uv, sets up the local libraries for the source builds (see the
psycopg2 note), creates the uv venv at Python 3.11, installs hammer editable, then Airflow
3.1.0 plus fab 3.6.3 plus python-ldap plus the real psycopg2, and prompts to create the
encrypted Postgres secrets.

### 4. tstech16c plugin, into the uv venv (replaces the course's `pip install -e .`)

Init the submodule as usual, but install it into the **venv**, not conda. Do **not** use
the course's `pip install -e .` here (that targets conda's 3.10).

```bash
git submodule update --init --recursive vlsi/hammer-tstech16c-plugin
uv pip install -e vlsi/hammer-tstech16c-plugin \
  --python vlsi/hammer/.venv/bin/python --no-deps
```

`hammer` is a namespace package, so this makes `hammer.tstech16c` importable under the venv
with no PYTHONPATH. (Worth folding into `uv_setup.sh` so a venv rebuild keeps it.)

### 5. Activate both environments to run

Conda first, venv on top so `python3` is the hammer venv:

```bash
source env.sh                  # conda: 3.10 toolchain
source vlsi/hammer/venv.sh     # uv venv: python3 = hammer 3.11, BWRC tools, secrets (GPG prompt)
which python3                  # sanity check: .../vlsi/hammer/.venv/bin/python3
```

If `which python3` shows the conda Python, you sourced in the wrong order, so re-run
`source vlsi/hammer/venv.sh` last.

### 6. Smoke tests (same as the course)

```bash
# VCS RTL simulation (chipyard flow; conda is enough, but the layered env is fine too)
cd sims/vcs && make CONFIG=RocketConfig && cd ../..

# synthesis input verilog (needs the venv)
cd vlsi && make CONFIG=RocketConfig verilog

# synthesis (needs the venv)
make CONFIG=RocketConfig syn
```

PAR won't run on `RocketConfig` because the top-level IOs don't match. That's expected;
it's a smoke test, not a tapeout config.

## Tapeout configs

| Config | Use for |
|---|---|
| `EE290SimConfig` | block-level IP HW/SW dev. No full-chip peripherals, faster sim, supports LOADMEM. |
| `EE290TapeoutConfig` | the real VLSI runs. Full peripherals and physical cells. |
| `EE290IOConfig` | top-level PAR debugging (RDL, IO ring). Minimal, runs faster. |

For RTL integration, change `EE290SimConfig` so it propagates to the tapeout config but
not the IO config. (The smoke tests above use `RocketConfig`; real runs use the EE290 ones.)

## Updating the tstech16c plugin

The plugin is under active development. The TAs will ask you to pull it, and it's the
first thing to try on tool errors during syn/PAR. For SledgeHammer, refresh it and
re-install into the venv:

```bash
cd vlsi/hammer-tstech16c-plugin
git checkout master && git pull
cd ../..
uv pip install -e vlsi/hammer-tstech16c-plugin --python vlsi/hammer/.venv/bin/python --no-deps
```

## psycopg2 needs pg_config (it's not a pip-vs-uv thing)

`psycopg2` (the source build, not `psycopg2-binary`) is a C extension that links libpq, so
it needs `pg_config` and the libpq headers. BWRC has runtime libpq but not `libpq-devel`,
and you have no root, so `pip install psycopg2` fails with "pg_config executable not found".
`uv pip install` would fail the same way. `uv_setup.sh` fixes it without root: it runs
`dnf download` on the `libpq-devel` and `libpq` RPMs, extracts them with `rpm2cpio | cpio`
into `~/pg_local`, and puts `pg_config` on PATH. The same pattern provides `libnsl` (Cadence
on RHEL 9) and `openldap-devel` (python-ldap). Let `uv_setup.sh` handle it. Don't fall back
to `psycopg2-binary`, whose bundled libpq/libssl clashes with the system's.

By-hand ordering, if you ever need it: get `pg_config` on PATH before `uv sync`; reinstall
the real psycopg2 with `--no-binary --reinstall` after Airflow (Airflow's constraints pull
in `psycopg2-binary`); and don't run `dnf download --resolve libpq-devel` (it skips libpq
and leaves a dangling `libpq.so`, so the link fails with `cannot find -lpq`).

## Don't merge the two Pythons

This is why we skip the course's pip-into-conda steps. Installing hammer into the conda env
would target Python 3.10 and bump conda's pydantic to satisfy hammer, which risks breaking
other Chipyard tooling. Hammer and the plugin stay in the uv venv (3.11); conda only
supplies the tool binaries. Running `ee290-vlsi` under conda's Python gives `No module named
'hammer.vlsi'`, so the venv has to be active.

## Daily use

```bash
cd <chipyard>
source env.sh
source vlsi/hammer/venv.sh    # GPG passphrase to load the DB secrets
cd vlsi
# make CONFIG=EE290TapeoutConfig verilog / syn / par ; or trigger the Airflow DAG / sledgehammer
```

Re-source both in every new shell.

## Useful tips

- `OBJ_DIR=<build_dir>` gives you separate build dirs under `vlsi/` (parallel runs).
- `LOADMEM=1` loads the simulation binary faster (with `EE290SimConfig`).

## Gotchas / troubleshooting

- **`No module named 'hammer.vlsi'`**: venv not active, or conda's `python3` is shadowing.
  Re-source `venv.sh` last and check `which python3`.
- **`No module named 'hammer.tstech16c'`**: plugin not installed in the venv (step 4), or a
  rebuild dropped it. Re-run the step-4 `uv pip install -e`.
- **syn hangs at "global incremental optimization"**: genus super-threading wedges at high
  thread counts. `design.yml` sets `vlsi.core.max_threads: 8` for this; drop to 1 if needed.
- **`make buildfile` emits `hammer.d` instead of the DAG**: it needs `vlsi.core.build_system:
  sledgehammer` to win the config merge (it loads after `tools.yml`).
- **Never run a bare `uv sync`** in the hammer venv: it resets to the lockfile and wipes the
  Airflow stack (installed outside the lock). Refresh the editable install with
  `uv pip install -e . --force-reinstall --no-deps`, or restore Airflow via `uv_setup.sh`.
- **Secrets** live GPG-encrypted in `<hammer>/.sledgehammer/airflow-secrets.env.gpg`.
  `venv.sh` decrypts and loads them on source. `airflow.cfg` ships with a blank connection.
- **The PD cache** lives in Postgres (`sledgehammer_studio.hammer_poc`), separate from the
  build dir and the Airflow metadata DB. Clearing the build dir or deleting DAG runs does
  not touch cached blobs.

---

**Environments:** Chipyard conda Python 3.10.14, SledgeHammer uv venv Python 3.11.15,
Airflow 3.1.0 plus providers-fab 3.6.3, uv 0.11.x.
**Updated:** 2026-06-27.
