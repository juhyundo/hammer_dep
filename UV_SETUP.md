# Chipyard + SledgeHammer Setup with uv (PostgreSQL + Airflow)

This document provides a **complete from-scratch guide** for setting up **Chipyard** with **Hammer (Sledgehammer)** VLSI framework, orchestrated by **Apache Airflow 3.x** with a **PostgreSQL** database backend, all managed by **[uv](https://github.com/astral-sh/uv)** instead of Poetry/Conda.

**What this setup provides:**
- **Chipyard**: RISC-V SoC design framework and build system
- **Hammer (Sledgehammer)**: VLSI flow driver that runs synthesis, place-and-route, simulation, etc.
- **Airflow 3.x**: Orchestrates the VLSI flow steps as DAGs (Directed Acyclic Graphs)
- **PostgreSQL backend**: Production-grade database for Airflow enabling parallel execution
- **uv**: Fast, modern Python package manager replacing pip/poetry/conda for Hammer deps

**Why uv instead of Poetry/Conda?**
- **10-100x faster** than pip/poetry for dependency resolution and installation
- **Single tool**: replaces pip, poetry, virtualenv, and pyenv
- **Simple installation**: one `curl` command, no Python bootstrap needed
- **Standard format**: uses PEP 621 `pyproject.toml` (no proprietary `[tool.poetry]` format)
- **Source builds just work**: `--no-binary` flag compiles C extensions from source cleanly

## Table of Contents

- [Prerequisites](#prerequisites)
- [Part 1: Hammer + Airflow + PostgreSQL Setup](#part-1-hammer--airflow--postgresql-setup)
  - [Step 0: Install Python (If Not Already Installed)](#step-0-install-python-if-not-already-installed)
  - [Step 0.3: Set Up GitHub SSH Access](#step-03-set-up-github-ssh-access)
  - [Step 0.4: Create Working Directory](#step-04-create-working-directory)
  - [Step 0.5: Clone Hammer Repository](#step-05-clone-hammer-repository)
  - [Step 1: Install uv](#step-1-install-uv)
  - [Step 1.5: Set Up pg\_config for psycopg2](#step-15-set-up-pg_config-for-psycopg2)
  - [Step 2: Create Virtual Environment and Install Dependencies](#step-2-create-virtual-environment-and-install-dependencies)
  - [Step 3: Install Apache Airflow](#step-3-install-apache-airflow)
  - [Step 4: Set Up PostgreSQL Connection](#step-4-set-up-postgresql-connection)
  - [Step 5: Configure Airflow](#step-5-configure-airflow)
  - [Step 6: Initialize Database](#step-6-initialize-database)
  - [Step 7: Start Airflow](#step-7-start-airflow)
- [Part 2: Chipyard Integration](#part-2-chipyard-integration)
  - [Step 8: Clone Chipyard](#step-8-clone-chipyard)
  - [Step 9: Chipyard Build Setup (Optional)](#step-9-chipyard-build-setup-optional)
  - [Step 10: Modify Hammer Files for Airflow Compatibility](#step-10-modify-hammer-files-for-airflow-compatibility)
  - [Step 11: Install libnsl Locally (RHEL 9)](#step-11-install-libnsl-locally-rhel-9--required-for-cadence-tools)
  - [Step 12: Update Airflow Configuration for DAGs](#step-12-update-airflow-configuration-for-dags)
  - [Step 13: Verify DAG Discovery](#step-13-verify-dag-discovery)
- [Available DAGs](#available-dags)
- [Usage](#usage)
- [Daily Usage](#daily-usage)
- [Relaunching (After Initial Setup)](#relaunching-after-initial-setup)
- [Clearing / Resetting a DAG Run](#clearing--resetting-a-dag-run)
- [uv Cheat Sheet (vs Poetry)](#uv-cheat-sheet-vs-poetry)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)
- [All Modifications Summary](#all-modifications-summary)
- [Directory Structure](#directory-structure)

---

## Prerequisites

1. **SSH Access to BWRC Machines**:
   - **Login Server**: `bwrcrdsl-1.eecs.berkeley.edu` (for initial connection)
   - **Compute Node**: You should be on a BWRC compute node (e.g., `bwrcix-3`, `bwrcix-4`, etc.)
   ```bash
   # First, SSH to login server
   ssh <username>@bwrcrdsl-1.eecs.berkeley.edu
   
   # Then SSH to any available compute node
   ssh <username>@bwrcix-3.eecs.berkeley.edu
   ```
   
   **Note**: Airflow should run on compute nodes, not the login server.

2. **GitHub Account** with SSH access configured (see Step 0.3)

3. **PostgreSQL Server**: Access to `barney.eecs.berkeley.edu:5433`
   
   **If the PostgreSQL server is down**, contact:
   - **Email**: anne_young@berkeley.edu
   - **System Admins**: bwrc-sysadmins@lists.eecs.berkeley.edu

4. **Database**: A PostgreSQL database must exist (e.g., `airflow_<username>`). You can verify this using:

```bash
psql -h barney.eecs.berkeley.edu -p 5433 -U <username> -l
```

5. **Python 3.10-3.13**: Required for Airflow 3.1.0

---

## Part 1: Hammer + Airflow + PostgreSQL Setup

### Step 0: Install Python (If Not Already Installed)

If Python 3.10-3.13 is not already available on your system, build from source:

```bash
cd ~
wget https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
tar -xzf Python-3.11.9.tgz
cd Python-3.11.9

# Configure and build (this may take 10-30 minutes)
./configure --prefix=$HOME/python311 --enable-optimizations --with-ensurepip=yes
make -j$(nproc)
make install

# Clean up (optional, saves ~380MB)
cd ~
rm -rf Python-3.11.9 Python-3.11.9.tgz

# Add to PATH
echo 'export PATH="$HOME/python311/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Verify
python3 --version
# Should show: Python 3.11.9
```

### Step 0.3: Set Up GitHub SSH Access

1. **Generate SSH Key** (if you don't already have one):
   ```bash
   ssh-keygen -t ed25519 -C "your_email@berkeley.edu"
   ```

2. **Start SSH Agent and Add Key**:
   ```bash
   eval "$(ssh-agent -s)"
   ssh-add ~/.ssh/id_ed25519
   ```

3. **Copy Your Public Key**:
   ```bash
   cat ~/.ssh/id_ed25519.pub
   ```

4. **Add SSH Key to GitHub**: Go to GitHub → Settings → SSH and GPG keys → New SSH key → Paste key

5. **Verify**:
   ```bash
   ssh -T git@github.com
   # Should show: Hi <username>! You've successfully authenticated...
   ```

### Step 0.4: Create Working Directory

```bash
mkdir -p /bwrcq/C/<username>
cd /bwrcq/C/<username>
```

**Note**: Replace `<username>` with your actual LDAP username. Your home directory may be at `/bwrcq/home/<username>` or `/bwrcq/C/<username>`.

### Step 0.5: Clone Hammer Repository

```bash
cd /bwrcq/C/<username>
git clone git@github.com:ucb-bar/hammer.git
cd hammer
```

### Step 1: Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

# Add to PATH (add to ~/.bashrc for persistence)
export PATH="$HOME/.local/bin:$PATH"

# Verify
uv --version
# Should show: uv 0.10.x or later
```

That's it — no Python bootstrap, no pip, no wheel needed.

### Step 1.5: Set Up pg_config for psycopg2

`psycopg2` (the real PostgreSQL adapter, **not** `psycopg2-binary`) must be compiled from C source. This requires `pg_config` and the PostgreSQL development headers, which are **not** installed system-wide on BWRC servers. We extract them locally from the `libpq-devel` RPM — no root access needed.

```bash
# Create a temp directory and download the RPMs
mkdir -p /tmp/pg_build_$$
cd /tmp/pg_build_$$
dnf download --resolve libpq-devel

# Extract the devel RPM (contains pg_config + headers)
mkdir -p ~/pg_local
rpm2cpio libpq-devel-*x86_64.rpm | (cd ~/pg_local && cpio -idmv)

# Extract the libpq RPM (contains libpq.so shared libraries)
# Find the non-devel libpq RPM filename, then extract it
LIBPQ_RPM=$(ls libpq-[0-9]*x86_64.rpm 2>/dev/null | head -1)
if [ -n "$LIBPQ_RPM" ]; then
    rpm2cpio "$LIBPQ_RPM" | (cd ~/pg_local && cpio -idmv)
fi

# Create the linker symlink
ln -sf libpq.so.5 ~/pg_local/usr/lib64/libpq.so

# Go back home BEFORE cleaning up (avoids "could not identify current directory" error)
cd ~
rm -rf /tmp/pg_build_$$

# Verify pg_config works
export PATH="$HOME/pg_local/usr/bin:$PATH"
pg_config --version
# Should show: PostgreSQL 13.x

pg_config --includedir
# Should show: /bwrcq/home/<username>/pg_local/usr/include

pg_config --libdir
# Should show: /bwrcq/home/<username>/pg_local/usr/lib64
```

**Make pg_config persistent** — add to `~/.bashrc`:

```bash
echo 'export PATH="$HOME/pg_local/usr/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Now `uv` (and pip) can build `psycopg2` from source whenever needed.

### Step 2: Create Virtual Environment and Install Dependencies

First, you need to convert the `pyproject.toml` from Poetry format to standard PEP 621 format. Replace the contents of `pyproject.toml` with:

```toml
[project]
name = "hammer-vlsi"
version = "0.0.0"
description = "Hammer is a physical design framework that wraps around vendor specific technologies and tools to provide a single API to create ASICs."
authors = [
    {name = "Colin Schmidt", email = "colin.schmidt@sifive.com"},
    {name = "Edward Wang", email = "edward.c.wang@compdigitec.com"},
    {name = "John Wright", email = "johnwright@eecs.berkeley.edu"},
]
maintainers = [
    {name = "Harrison Liew", email = "harrisonliew@berkeley.edu"},
    {name = "Daniel Grubb", email = "dpgrubb@berkeley.edu"},
]
readme = "README.md"
requires-python = ">=3.9"
dependencies = [
    "pydantic>=2.12.2,<3",
    "PyYAML>=6.0,<7",
    "ruamel.yaml>=0.17.21,<0.18",
    "networkx>=3.0,<4",
    "numpy>=1.23.0,<2",
    "sphinxcontrib-mermaid>=0.8.1,<0.9",
    "psycopg2>=2.9.11,<3",
    "asyncpg>=0.30.0,<1",
]

[project.optional-dependencies]
asap7-gdstk = ["gdstk>=0.9.0,<0.10"]
asap7 = ["gdspy==1.4"]

[project.scripts]
hammer-generate-mdf = "hammer.shell.hammer_generate_mdf:main"
get-config = "hammer.shell.get_config:main"
hammer-vlsi = "hammer.shell.hammer_vlsi:main"
hammer-shell-test = "hammer.shell.hammer_shell_test:main"
readlink-array = "hammer.shell.readlink_array:main"
yaml2json = "hammer.shell.yaml2json:main"
asap7_gds_scale = "hammer.technology.asap7.gds_scale:main"

[project.urls]
Repository = "https://github.com/ucb-bar/hammer"

[dependency-groups]
dev = [
    "pytest>=7.1,<8",
    "pyright>=1.1,<2",
    "types-PyYAML>=6.0.0,<7",
    "tox>=3.25.1,<4",
    "Sphinx>=5.1.1,<6",
    "sphinx-rtd-theme>=1.0.0,<2",
    "myst-parser>=0.18.0,<0.19",
    "sphinx-jsonschema>=1.19.1,<2",
    "sphinx-rtd-size>=0.2.0,<0.3",
    "pylint>=2.15,<3",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["hammer"]

[tool.tox]
legacy_tox_ini = """
[tox]
envlist = py39,py310
isolated_build = True

[testenv]
deps = pytest
commands =
  pytest tests/ {posargs}
"""

[tool.pylint.format]
max-line-length = 120

[tool.pylint.reports]
output-format = "text"
reports = true

[tool.pylint."messages control"]
disable = ["no-else-return", "len-as-condition"]

[tool.pyright]
exclude = ["doc/**", "e2e/**", "test_scripts/**"]
include = ["hammer", "tests"]
reportMissingImports = true
typeCheckingMode = "standard"
```

**Key differences from Poetry format**:
- `[tool.poetry]` → `[project]` (PEP 621 standard)
- `[tool.poetry.dependencies]` → `[project.dependencies]` (PEP 508 specifiers)
- `[tool.poetry.scripts]` → `[project.scripts]`
- `[tool.poetry.dev-dependencies]` → `[dependency-groups]` dev
- Build backend: `poetry-core` → `hatchling`

**Important**: Make sure you completed [Step 1.5](#step-15-set-up-pg_config-for-psycopg2) first — `pg_config` must be on your `PATH` so `psycopg2` can compile from source.

Now create the venv and install:

```bash
# Ensure pg_config is on PATH
export PATH="$HOME/pg_local/usr/bin:$HOME/.local/bin:$PATH"
pg_config --version   # Should show: PostgreSQL 13.x

# Pin Python version
uv python pin 3.11

# Create virtual environment using your Python
uv venv --python $HOME/python311/bin/python3

# Lock dependencies (psycopg2 will be built from source)
uv lock

# Install everything (project + dev dependencies)
uv sync --group dev

# Verify
source .venv/bin/activate
hammer-vlsi -h
python3 -c "import psycopg2; print('psycopg2:', psycopg2.__version__)"
python3 -c "import asyncpg; print('asyncpg:', asyncpg.__version__)"
```

**Note**: `uv sync` resolved and installed all 67+ packages in under 1 minute. Poetry typically takes 5-15 minutes for the same operation. `psycopg2` is compiled from C source during install (~20 seconds).

### Step 3: Install Apache Airflow

Apache Airflow is installed separately (not in `pyproject.toml`) using Airflow's constraint file for version compatibility:

```bash
# Make sure you're in the hammer directory with venv activated
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)
export PATH="$HOME/pg_local/usr/bin:$HOME/.local/bin:$PATH"

# Define versions
AIRFLOW_VERSION=3.1.0
PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
CONSTRAINT_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

# Remove doc packages that conflict with Airflow
pip uninstall -y myst-parser mdit-py-plugins markdown-it-py 2>/dev/null || true

# Install Airflow with constraints (uv pip is ~10x faster than pip)
uv pip install "apache-airflow==${AIRFLOW_VERSION}" --constraint "${CONSTRAINT_URL}"

# Reinstall psycopg2 from source (Airflow may have swapped it for psycopg2-binary)
uv pip install psycopg2==2.9.11 --no-binary psycopg2 --reinstall

# Verify
airflow version
# Should show: 3.1.0

python3 -c "import psycopg2; print('psycopg2:', psycopg2.__version__)"
# Should show: psycopg2: 2.9.11 (dt dec pq3 ext lo64)
```

### Step 4: Set Up PostgreSQL Connection

#### 4.1 Get PostgreSQL Password

1. Check your @berkeley.edu email for a message from the server administrator
2. Click the link and retrieve your PostgreSQL password

#### 4.2 Generate airflow.cfg

```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)

# This creates airflow.cfg
airflow config list > /dev/null 2>&1

# Verify it was created
ls -la airflow.cfg
```

#### 4.3 Update airflow.cfg

Edit `airflow.cfg` and update the `[database]` section:

```ini
[database]
sql_alchemy_conn = postgresql+psycopg2://<username>:YOUR_PASSWORD@barney.eecs.berkeley.edu:5433/<database>
sql_alchemy_pool_size = 10
sql_alchemy_max_overflow = 20
sql_alchemy_engine_args = {"pool_pre_ping": true}
```

Replace `<username>` with your LDAP username and `YOUR_PASSWORD` with your actual PostgreSQL password.

#### 4.4 Test Connection

```bash
airflow db check
# Should show: Connection successful.
```

### Step 5: Configure Airflow

Edit `airflow.cfg` with these critical settings:

```ini
[core]
executor = LocalExecutor
parallelism = 30
max_active_tasks_per_dag = 30
# For basic setup, use a dags/ directory. For chipyard integration (Part 2), change to hammer/shell
dags_folder = /bwrcq/C/<username>/hammer/dags

[api]
# Uncomment and set base_url (required for task execution in Airflow 3.x)
base_url = http://localhost:8080

[scheduler]
task_queued_timeout = 1200.0
job_heartbeat_sec = 10
scheduler_heartbeat_sec = 10
```

Create the DAGs folder:

```bash
mkdir -p dags
```

Verify configuration:

```bash
airflow config get-value database sql_alchemy_conn
airflow config get-value core executor
airflow config get-value core dags_folder
```

### Step 6: Initialize Database

```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)

# Run migrations
airflow db migrate

# Verify
airflow db check
airflow db check-migrations
```

### Step 7: Start Airflow

#### Option A: Standalone (Recommended)

```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)

airflow standalone
```

Wait for `Airflow is ready`. The auto-generated admin password is in `simple_auth_manager_passwords.json.generated`.

**Note**: In Airflow 3.x, the `airflow users create` CLI command has been removed. `airflow standalone` creates the admin user automatically.

#### Option B: Separate Processes

**Terminal 1 - Scheduler:**
```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)
airflow scheduler
```

**Terminal 2 - API Server:**
```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)
airflow api-server --port 8080
```

**Note**: In Airflow 3.x, the web UI is served by `airflow api-server`, not `airflow webserver`.

#### Access the UI

**From Remote Desktop (NoMachine/X2Go):**
- URL: http://localhost:8080
- Username: `admin`
- Password: See `simple_auth_manager_passwords.json.generated`

**Via SSH Port Forwarding (from your laptop):**
```bash
ssh -A -L 8080:localhost:8080 -J <username>@bwrcrdsl-1.eecs.berkeley.edu <username>@<compute_node>.eecs.berkeley.edu
```
Then open http://localhost:8080 in your browser.

---

## Part 2: Chipyard Integration

This section sets up Chipyard and configures Hammer's Airflow DAGs for the Sledgehammer VLSI flow. After completing Part 1, you have Hammer + Airflow + PostgreSQL working. Now we add:

- Chipyard (cloned into the hammer directory)
- Hammer source modifications for Airflow compatibility
- libnsl.so.1 for Cadence tools (RHEL 9 workaround)
- Config files for synthesis and PAR
- Sledgehammer DAGs (`Sledgehammer_demo_gcd`, `Sledgehammer_demo_rocket`)

### Step 8: Clone Chipyard

```bash
cd /bwrcq/C/<username>/hammer
git clone git@github.com:ucb-bar/chipyard.git chipyard
```

Add `chipyard/` to `.gitignore` (it's a separate git repository):

```bash
echo "chipyard/" >> .gitignore
```

### Step 9: Chipyard Build Setup (Optional)

> **Note**: Chipyard's `build-setup.sh` initializes submodules, builds the RISC-V toolchain, and sets up the conda environment. This takes **30+ minutes** and requires **15GB+ disk space**. It is **only needed for chipyard-based designs** (e.g., RocketTile). The GCD demo uses Hammer's built-in `e2e/` test configs and does **not** require a built chipyard.

If you need to build chipyard-based designs:

```bash
cd /bwrcq/C/<username>/hammer/chipyard

# Source BWRC environment for EDA tool paths
source /tools/C/ee290-sp25/bwrc-env.sh

# Run build setup (skip FireSim steps 6-9)
./build-setup.sh riscv-tools -s 6 -s 7 -s 8 -s 9
```

For just the GCD demo and Sledgehammer DAGs, **skip this step** — the chipyard clone is sufficient.

### Step 10: Modify Hammer Files for Airflow Compatibility

Several Hammer source files need modification to work correctly within Airflow. These changes are **required** — without them, tasks will crash the Airflow worker process or fail to execute.

#### 10.1 Remove `sys.exit()` from `cli_driver.py`

**File**: `hammer/vlsi/cli_driver.py` (at the bottom of the file)

**Problem**: `CLIDriver.main()` calls `sys.exit()` at the end of execution. This terminates the entire Python process, which in Airflow's case is the worker — causing the task to be marked as `failed` even if the VLSI step succeeded.

**Fix**: Replace the last two lines:

```python
# Change from:
#     sys.exit(1)
# To:
      return 1

# Change from:
#     sys.exit(self.run_main_parsed(vars(parser.parse_args(args))))
# To:
      return self.run_main_parsed(vars(parser.parse_args(args)))
```

#### 10.2 Replace `hammer_vlsi.py` with DAG definitions

**File**: `hammer/shell/hammer_vlsi.py`

The stock `hammer_vlsi.py` is a 9-line script that just calls `CLIDriver().main()`. Replace it entirely with the Sledgehammer DAG definitions that define the `Sledgehammer_demo_gcd` and `Sledgehammer_demo_rocket` DAGs.

The modified file includes:
- **`LD_LIBRARY_PATH` setup**: Module-level code that adds `~/libnsl_local/usr/lib64` to `LD_LIBRARY_PATH` so Cadence tools can find `libnsl.so.1`. This **must** be in the DAG file — Airflow worker subprocesses don't inherit the parent shell's environment.
- **`run_cli_driver()` wrapper**: Catches `SystemExit` from `CLIDriver().main()` to prevent killing the Airflow worker
- **`get_param()` helper**: Reads DAG parameters from `conf` (runtime) or `params` (defaults), fixing the issue where triggering without config skips all tasks
- **`AIRFlow` class**: GCD demo configuration with absolute paths to `e2e/` configs
- **`AIRFlow_rocket` class**: RocketTile demo configuration
- **Two DAG definitions**: `Sledgehammer_demo_gcd` and `Sledgehammer_demo_rocket` with branching task flows

**Important**: Update the absolute paths in the `AIRFlow` and `AIRFlow_rocket` classes to match your installation:

```python
# In AIRFlow.__init__():
self.vlsi_dir = '/bwrcq/C/<username>/hammer/e2e'  # ← your path

# In AIRFlow_rocket.__init__():
self.vlsi_dir = '/bwrcq/C/<username>/hammer/e2e'   # ← your path
self.specs_abs = '/bwrcq/C/<username>/hammer/specs' # ← your path
```

> **Tip**: You can copy the modified `hammer_vlsi.py` from a reference setup (e.g., `/bwrcq/home/lawrencejrhee/hammer_uv/hammer/shell/hammer_vlsi.py`) and only update the absolute paths.

#### 10.3 Comment out QRC tech file in Genus

**File**: `hammer/synthesis/genus/__init__.py` (around line 231)

**Problem**: The QRC tech file setting can cause Genus to fail if QRC files are not available for the PDK (Sky130 has no QRC). Commenting it out prevents failures during synthesis.

**Fix**: Comment out lines ~231–233:

```python
# Commented out QRC tech file setting - may cause issues if QRC is needed in the future
# if len(qrc_files) > 0:
#     verbose_append("set_db qrc_tech_file {{ {files} }}".format(
#         files=qrc_files[0]
#     ))
```

#### 10.4 Update e2e config files with absolute paths

Airflow workers do not execute from the `e2e/` directory, so all source file references must use **absolute paths**.

**File**: `e2e/configs-design/gcd/common.yml`

```yaml
synthesis.inputs:
  top_module: "gcd"
  input_files:
    - "/bwrcq/C/<username>/hammer/e2e/src/gcd.v"
```

**File**: `e2e/configs-design/gcd/sim-rtl.yml`

```yaml
sim.inputs:
  level: "rtl"
  input_files:
    - "/bwrcq/C/<username>/hammer/e2e/src/gcd.v"
    - "/bwrcq/C/<username>/hammer/e2e/src/gcd_tb.v"
```

#### 10.5 Create syn.yml and par.yml for GCD

The GCD demo needs synthesis and PAR config files that don't exist in stock Hammer.

**File**: `e2e/configs-design/gcd/syn.yml` (create this file)

```yaml
# Specify Global Variables
clockPeriod: &CLK_PERIOD "20.0ns"
clockPeriodby5: &CLK_PERIOD_BY_5 "4.0"
verilogSrc: &VERILOG_SRC
  - "/bwrcq/C/<username>/hammer/e2e/src/gcd.v"

vlsi.inputs.clocks: [
  {name: "clk", period: *CLK_PERIOD, uncertainty: "0.1ns"}
]

vlsi.inputs.delays: [
  {name: "mem*", clock: "clk", direction: "input", delay: *CLK_PERIOD_BY_5}
]

synthesis.inputs:
  top_module: "gcd"
  input_files: *VERILOG_SRC

synthesis.genus:
  use_qrc: false
  qrc_files: []
  generate_reports: true
  generate_final_netlist: true
  timing_driven: true
  clock_gating_mode: "empty"

technology.sky130:
  synthesis_tool: "genus"
```

**File**: `e2e/configs-design/gcd/par.yml` (create this file)

```yaml
# Place and Route configuration for GCD design
# PAR input is dynamically generated by syn_to_par() from synthesis outputs.
```

### Step 11: Install libnsl Locally (RHEL 9 — Required for Cadence Tools)

RHEL 9 removed `libnsl.so.1`, which Cadence tools (Genus, Innovus) depend on. Without it, synthesis and PAR fail with `genus: error while loading shared libraries: libnsl.so.1`.

```bash
mkdir -p /tmp/libnsl_build
cd /tmp/libnsl_build
dnf download libnsl

mkdir -p ~/libnsl_local
rpm2cpio libnsl-*x86_64.rpm | (cd ~/libnsl_local && cpio -idmv)

# Verify
ls ~/libnsl_local/usr/lib64/libnsl.so.1

cd ~
rm -rf /tmp/libnsl_build
```

> **IMPORTANT**: Do **NOT** copy Sky130 PDK files (`__init__.py`, `sram-cache.json`, `sram_compiler/__init__.py`) from other reference setups. They may be from a different Hammer version with incompatible APIs (e.g., importing `TechConfig` which doesn't exist in stock Hammer), causing `ImportError` at runtime. The stock Sky130 files work as-is for the GCD demo.

### Step 12: Update Airflow Configuration for DAGs

Now that the DAGs are in `hammer/shell/hammer_vlsi.py`, update `airflow.cfg`:

#### 12.1 Point `dags_folder` to hammer/shell

```ini
[core]
dags_folder = /bwrcq/C/<username>/hammer/hammer/shell
```

#### 12.2 Add Security Keys (CRITICAL for Airflow 3.x)

Without these, the scheduler cannot communicate with the API server and **all tasks will fail** with `Invalid auth token: Signature verification failed`.

Generate keys:

```bash
# Generate Fernet key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Generate secret key
python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(16)).decode())"

# Generate JWT secret
python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(16)).decode())"
```

Add to `airflow.cfg`:

```ini
[core]
fernet_key = <your_generated_fernet_key>
internal_api_secret_key = <your_generated_secret_key>

[api]
secret_key = <same_as_internal_api_secret_key>  # MUST match internal_api_secret_key

[api_auth]
jwt_secret = <your_generated_jwt_secret>
```

> **CRITICAL**: `internal_api_secret_key` and `secret_key` **must be the same value**. If they don't match, tasks will crash immediately with signature verification errors.

#### 12.3 Run DB Migrate

```bash
source venv.sh
airflow db migrate
```

### Step 13: Verify DAG Discovery

```bash
source venv.sh

# Reserialize DAGs
airflow dags reserialize

# List DAGs
airflow dags list | grep Sledgehammer
# Should show:
# Sledgehammer_demo_gcd
# Sledgehammer_demo_rocket

# Check for import errors
airflow dags list-import-errors
# Should show: No data found
```

---

## Available DAGs

### Sledgehammer_demo_gcd

GCD (Greatest Common Divisor) design workflow.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `clean` | boolean | `False` | Clean the build directory |
| `build` | boolean | `True` | Run the build step |
| `sim_rtl` | boolean | `True` | Run RTL simulation |
| `syn` | boolean | `True` | Run logic synthesis |
| `par` | boolean | `True` | Run place and route |

**Default config**: design=`gcd`, PDK=`sky130`, tools=`cm`, env=`bwrc`

### Sledgehammer_demo_rocket

RocketTile design workflow (includes SRAM generation step).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `clean` | boolean | `False` | Clean the build directory |
| `build` | boolean | `True` | Run the build step |
| `sim_rtl` | boolean | `True` | Run RTL simulation |
| `sram_generator` | boolean | `True` | Generate SRAM macros |
| `syn` | boolean | `True` | Run logic synthesis |
| `par` | boolean | `True` | Run place and route |

**Default config**: design=`intel2x2`, PDK=`intech22`, tools=`cm`, env=`intel2x2`

### DAG Task Flow

```
start → clean → build_decider ──→ build → sim_or_syn_decide ──→ sim_rtl → exit_
                     │                            │
                     │                            └──→ syn_decider → syn → par_decider → par → exit_
                     │
                     └──→ exit_ (if no steps enabled)
```

The Rocket DAG adds `sram_decider → sram_generator` between `sim_or_syn_decide` and `syn_decider`.

### IMPORTANT: `sim_rtl` and `syn/par` are mutually exclusive

The `sim_or_syn_decide` branch checks `sim_rtl` **first**. If `sim_rtl=True`, it takes the simulation path and **skips** synthesis/PAR. To run synthesis and PAR, set `sim_rtl=False`.

---

## Usage

### Triggering DAGs via CLI

```bash
source venv.sh

# GCD - Full default flow (build + sim_rtl)
airflow dags trigger Sledgehammer_demo_gcd

# GCD - Build + Synthesis + PAR (skip sim)
airflow dags trigger Sledgehammer_demo_gcd --conf '{"sim_rtl": false}'

# GCD - Build only
airflow dags trigger Sledgehammer_demo_gcd --conf '{"sim_rtl": false, "syn": false, "par": false}'

# GCD - Full flow with clean
airflow dags trigger Sledgehammer_demo_gcd --conf '{"clean": true}'

# Rocket - Build + SRAM + Synthesis
airflow dags trigger Sledgehammer_demo_rocket --conf '{"sim_rtl": false}'
```

### Triggering via Web UI

1. Open browser to `http://localhost:8080`
2. Find your DAG in the list
3. Unpause the DAG (toggle the switch)
4. Click the **Play** button (▶)
5. (Optional) Enter JSON config: `{"sim_rtl": false, "syn": true, "par": true}`
6. Click "Trigger"

### Checking Status

```bash
source venv.sh

# List DAGs
airflow dags list

# Check import errors
airflow dags list-import-errors

# Check task states for a run
airflow tasks states-for-dag-run Sledgehammer_demo_gcd "<run_id>"
```

---

## Daily Usage

### Quick Start (after initial setup)

```bash
cd /bwrcq/C/<username>/hammer
source venv.sh    # activates venv + sets AIRFLOW_HOME + sources BWRC env
airflow standalone
```

### Convenience Script: `venv.sh`

Create this file in the hammer root:

```bash
source ./.venv/bin/activate

# Set AIRFLOW_HOME to current directory (Hammer root)
export AIRFLOW_HOME=$(pwd)

# Ensure uv and pg_config are on PATH
export PATH="$HOME/pg_local/usr/bin:$HOME/.local/bin:$PATH"

# Source BWRC environment for EDA tools (VCS, Genus, Innovus, etc.)
if [ -f /tools/C/ee290-sp25/bwrc-env.sh ]; then
    source /tools/C/ee290-sp25/bwrc-env.sh
    echo "BWRC EDA environment sourced."
fi

# RHEL 9 workaround: Cadence tools need libnsl.so.1 (removed in RHEL 9)
if [ -f "$HOME/libnsl_local/usr/lib64/libnsl.so.1" ]; then
    export LD_LIBRARY_PATH="$HOME/libnsl_local/usr/lib64:${LD_LIBRARY_PATH:-}"
fi

echo "Virtual environment activated."
echo "AIRFLOW_HOME set to: $AIRFLOW_HOME"
```

Usage: `source venv.sh`

---

## Relaunching (After Initial Setup)

```bash
# 1. Navigate to hammer directory
cd /bwrcq/C/<username>/hammer

# 2. Source the convenience script
source venv.sh

# 3. Start Airflow
airflow standalone

# Check password in: simple_auth_manager_passwords.json.generated
```

---

## Clearing / Resetting a DAG Run

### Clear all runs for a DAG

```bash
source venv.sh
airflow tasks clear Sledgehammer_demo_gcd --yes
```

### Delete all DAG run history (fresh start)

```bash
airflow dags delete Sledgehammer_demo_gcd --yes
# The DAG will be re-imported on the next scheduler scan
```

### Via Web UI

1. Go to the DAG's Grid view
2. Click on the specific DAG run
3. Click "Clear" to reset all tasks in that run

---

## uv Cheat Sheet (vs Poetry)

| Task | Poetry | uv |
|------|--------|-----|
| Install tool | `curl -sSL https://install.python-poetry.org \| python3 -` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Create venv | `poetry config virtualenvs.in-project true` | `uv venv` |
| Install deps | `poetry install` | `uv sync` |
| Install with dev | `poetry install` (auto) | `uv sync --group dev` |
| Add dependency | `poetry add package` | `uv add package` |
| Add dev dependency | `poetry add --dev package` | `uv add --group dev package` |
| Update lockfile | `poetry lock` | `uv lock` |
| Run command | `poetry run cmd` | `uv run cmd` |
| Activate venv | `poetry shell` | `source .venv/bin/activate` |
| Show installed | `poetry show` | `uv pip list` |
| Pin Python | N/A | `uv python pin 3.11` |
| Install Python | Need pyenv | `uv python install 3.11` |

### Key Advantages of uv

1. **10-100x faster**: Dependency resolution in milliseconds, not minutes
2. **Single binary**: No Python bootstrap required to install
3. **PEP 621 standard**: `pyproject.toml` uses the standard `[project]` format
4. **Built-in Python management**: Can install Python versions itself
5. **Lockfile**: `uv.lock` is cross-platform by default
6. **Source builds**: `--no-binary` flag compiles C extensions from source cleanly

---

## Verification

### Quick Check

```bash
cd /bwrcq/C/<username>/hammer
source venv.sh

# Test hammer
hammer-vlsi -h

# Test database
airflow db check
airflow db check-migrations

# Test postgres connection
./test_postgres_connection.sh

# Run hammer tests
pytest tests/ -m "not long" -v
```

### Verify Metadata Storage

```bash
python3 << 'EOF'
from sqlalchemy import create_engine, text
from airflow.configuration import conf

engine = create_engine(conf.get('database', 'sql_alchemy_conn'))
with engine.connect() as conn:
    result = conn.execute(text("SELECT COUNT(*) FROM dag_run"))
    print(f"DAG runs: {result.fetchone()[0]}")
    
    result = conn.execute(text("SELECT COUNT(*) FROM task_instance"))
    print(f"Task instances: {result.fetchone()[0]}")
    
    result = conn.execute(text("SELECT COUNT(*) FROM task_instance WHERE state = 'success'"))
    print(f"Successful tasks: {result.fetchone()[0]}")
EOF
```

---

## Troubleshooting

### "uv: command not found"

```bash
export PATH="$HOME/.local/bin:$PATH"
# Add to ~/.bashrc for persistence
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

### psycopg2 fails to build from source

**Error**: `pg_config executable not found`

This means `pg_config` is not on your `PATH`. **Do NOT switch to `psycopg2-binary`**. Instead, set up `pg_config` locally (see [Step 1.5](#step-15-set-up-pg_config-for-psycopg2)):

```bash
# 1. Make sure pg_config is on PATH
export PATH="$HOME/pg_local/usr/bin:$PATH"
pg_config --version   # Should show PostgreSQL 13.x

# 2. Rebuild psycopg2 from source
uv pip install psycopg2==2.9.11 --no-binary psycopg2 --reinstall

# 3. Verify it's the real psycopg2 (compiled from C source)
python3 -c "import psycopg2; print('psycopg2:', psycopg2.__version__)"
# Should show: 2.9.11 (dt dec pq3 ext lo64)
```

**If `dnf download` doesn't work** in Step 1.5, contact:
- **Email**: anne_young@berkeley.edu
- **System Admins**: bwrc-sysadmins@lists.eecs.berkeley.edu
- **Request**: `libpq-devel` package (provides `pg_config` and PostgreSQL headers)

### Connection Refused to PostgreSQL

1. Verify you're on a compute node, not the login server
2. Check server: `ping barney.eecs.berkeley.edu`
3. Check port: `telnet barney.eecs.berkeley.edu 5433`
4. If server is down, contact: anne_young@berkeley.edu or bwrc-sysadmins@lists.eecs.berkeley.edu

### Authentication Failed

1. Verify username and password in `airflow.cfg` connection string
2. Check for extra spaces when copying password
3. Contact sysadmins if password doesn't work

### Tasks Not Starting / Network Unreachable

Ensure `base_url` is set in `[api]` section of `airflow.cfg`:
```ini
[api]
base_url = http://localhost:8080
```

### Port Already in Use

```bash
# Check what's using the port
lsof -i :8080
# Or use a different port
airflow api-server --port 8090
# Then update airflow.cfg: base_url = http://localhost:8090
```

### Migrations Pending

```bash
airflow db migrate
```

### Tasks fail with "Invalid auth token: Signature verification failed"

**Cause**: One or more security keys in `airflow.cfg` are missing or inconsistent.

**Fix (check in order)**:
1. Ensure `fernet_key`, `internal_api_secret_key`, and `secret_key` are all set (see [Step 12.2](#122-add-security-keys-critical-for-airflow-3x))
2. Ensure `internal_api_secret_key` and `secret_key` are the **same value**
3. Add `[api_auth] jwt_secret` if missing

### Tasks fail with "hammer-shell-test returned non-zero exit status" or "No module named 'hammer'"

**Cause**: The `hammer` package is not discoverable. The editable install `.pth` file may be stale.

**Fix**:
```bash
cd /bwrcq/C/<username>/hammer
uv pip install -e .
hammer-shell-test
# Should print: "hammer-shell appears to be on the path"
```

### Tasks crash with `RecursionError: maximum recursion depth exceeded`

**Cause**: The `run_cli_driver()` wrapper is calling itself instead of `CLIDriver().main()`.

**Fix**: Ensure `run_cli_driver()` calls `CLIDriver().main()` internally, NOT `run_cli_driver()`.

### `syn`, `par` tasks are skipped even though they're set to True

**This is expected.** The `sim_or_syn_decide` branch treats `sim_rtl` and `syn/par` as **mutually exclusive**. To run syn/par:

```bash
airflow dags trigger Sledgehammer_demo_gcd --conf '{"sim_rtl": false}'
```

### `genus: error while loading shared libraries: libnsl.so.1`

**Cause**: RHEL 9 removed `libnsl.so.1`. Cadence Genus and Innovus depend on it.

**Fix**: Install `libnsl.so.1` locally (see [Step 11](#step-11-install-libnsl-locally-rhel-9--required-for-cadence-tools)) AND ensure `LD_LIBRARY_PATH` is set in `hammer_vlsi.py` (see [Step 10.2](#102-replace-hammer_vlsipy-with-dag-definitions)).

Setting `LD_LIBRARY_PATH` only in `venv.sh` is **not enough** — Airflow worker subprocesses don't inherit it. The path must also be set in the DAG file itself.

### DAGs not appearing

1. Check import errors: `airflow dags list-import-errors`
2. Verify `dags_folder` in `airflow.cfg` points to `hammer/shell/`
3. Test import manually: `python3 hammer/shell/hammer_vlsi.py`
4. Verify hammer is importable: `python3 -c "from hammer.vlsi import CLIDriver"`
5. Run `airflow dags reserialize` to force a re-scan

### `airflow db init` doesn't work

Airflow 3.x removed `db init`. Use `airflow db migrate` instead — it's idempotent and safe to run any number of times.

### Want to Reset Everything

```bash
cd /bwrcq/C/<username>/hammer
rm -rf .venv uv.lock airflow.cfg logs/ chipyard/
export PATH="$HOME/pg_local/usr/bin:$HOME/.local/bin:$PATH"
uv venv --python $HOME/python311/bin/python3
uv lock
uv sync --group dev
# Then re-do Steps 3-7 and Part 2
```

---

## All Modifications Summary

### Files Modified from Stock Hammer

| File | Change | Why |
|------|--------|-----|
| `pyproject.toml` | Converted from Poetry to PEP 621 for uv | uv compatibility |
| `hammer/vlsi/cli_driver.py` | `sys.exit(1)` → `return 1`; `sys.exit(self.run_main_parsed(...))` → `return self.run_main_parsed(...)` | Prevent `sys.exit()` from killing Airflow workers |
| `hammer/shell/hammer_vlsi.py` | Replaced with ~1090-line Sledgehammer DAG definitions including `LD_LIBRARY_PATH` setup | Airflow DAG file with libnsl workaround, `run_cli_driver()` wrapper, two DAG definitions |
| `hammer/synthesis/genus/__init__.py` | Commented out QRC tech file lines ~231-233 | Sky130 has no QRC; prevents Genus errors |
| `e2e/configs-design/gcd/common.yml` | Relative paths → absolute paths | Airflow workers don't run from e2e/ |
| `e2e/configs-design/gcd/sim-rtl.yml` | Relative paths → absolute paths | Airflow workers don't run from e2e/ |
| `airflow.cfg` | dags_folder, security keys, PostgreSQL connection | Airflow configuration |
| `.gitignore` | Added chipyard/, airflow.cfg, logs/, wheels/ | Git cleanliness |
| `venv.sh` | Created convenience script with libnsl `LD_LIBRARY_PATH` | Easy environment activation |

### Files Created (not in stock Hammer)

| File | Purpose |
|------|---------|
| `e2e/configs-design/gcd/syn.yml` | Synthesis constraints: clock period (20ns), input delays, QRC disabled |
| `e2e/configs-design/gcd/par.yml` | PAR config placeholder |

### Files NOT Modified (left as-is from stock Hammer)

| File | Note |
|------|------|
| `hammer/technology/sky130/__init__.py` | Stock Sky130 PDK — do NOT replace with copies from other setups |
| `hammer/technology/sky130/sram-cache.json` | Stock SRAM cache — do NOT replace |
| `hammer/technology/sky130/sram_compiler/__init__.py` | Stock SRAM compiler — do NOT replace |

### External Dependencies Installed (not in the repo)

| Item | Location | Purpose |
|------|----------|---------|
| `libnsl.so.1` | `~/libnsl_local/usr/lib64/` | RHEL 9 workaround — Cadence tools need this library. Extracted from RPM. |

---

## Directory Structure

```
hammer/                               # AIRFLOW_HOME
├── airflow.cfg                       # Airflow configuration (CRITICAL)
├── pyproject.toml                    # PEP 621 project config (for uv)
├── uv.lock                           # uv lockfile
├── .venv/                            # Virtual environment
├── venv.sh                           # Convenience activation script
├── chipyard/                         # Chipyard repository (cloned in Step 8)
├── logs/                             # Airflow task logs
├── simple_auth_manager_passwords...  # Auto-generated admin password
├── hammer/                           # Hammer Python package
│   ├── shell/
│   │   └── hammer_vlsi.py            # ★ DAG definitions (Sledgehammer_demo_gcd, _rocket)
│   ├── vlsi/
│   │   └── cli_driver.py             # ★ CLIDriver (modified: sys.exit → return)
│   ├── synthesis/
│   │   └── genus/
│   │       └── __init__.py           # ★ Genus plugin (modified: QRC workaround)
│   └── technology/
│       └── sky130/                   # Stock Sky130 PDK (do NOT replace)
└── e2e/                              # End-to-end configs
    ├── configs-env/                  # Environment configs (bwrc-env.yml)
    ├── configs-pdk/                  # PDK configs (sky130.yml)
    ├── configs-tool/                 # Tool configs (cm.yml)
    ├── configs-design/               # Design configs
    │   └── gcd/
    │       ├── common.yml            # ★ Design config (absolute paths)
    │       ├── sim-rtl.yml           # ★ Simulation config (absolute paths)
    │       ├── syn.yml               # ★ Synthesis config (CREATED — not in stock)
    │       ├── par.yml               # ★ PAR config (CREATED — not in stock)
    │       └── sky130.yml            # PDK-specific design config
    └── src/
        ├── gcd.v                     # GCD design source
        └── gcd_tb.v                  # GCD testbench
```

Files marked with ★ were modified from the stock Hammer repository.

---

## Best Practices

1. **All 4 security keys**: Always set `fernet_key`, `internal_api_secret_key`, `secret_key`, AND `jwt_secret`
2. **Port consistency**: `[api] base_url` must match the actual port Airflow runs on
3. **Source BWRC env**: Always source the EDA tool environment before starting Airflow
4. **Set AIRFLOW_HOME**: Every terminal that runs `airflow` commands must have `AIRFLOW_HOME` set
5. **No `sys.exit()` in tasks**: Never use `sys.exit()` in Airflow task code
6. **Use absolute paths in YAML configs**: Airflow workers don't run from the e2e directory
7. **Understand `sim_rtl` vs `syn/par` exclusivity**: The DAG branches — you can run sim OR syn/par, not both
8. **Use `airflow db migrate` freely**: It's idempotent — safe to run anytime
9. **Verify editable installs after moving repos**: Re-run `uv pip install -e .` if you move the directory

---

## References

- [Chipyard Documentation](https://chipyard.readthedocs.io/)
- [Hammer VLSI Documentation](https://hammer-vlsi.readthedocs.io/)
- [Apache Airflow 3.x Documentation](https://airflow.apache.org/docs/)
- [uv Documentation](https://docs.astral.sh/uv/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [psycopg2 Documentation](https://www.psycopg.org/docs/)

---

## Support

For issues specific to SledgeHammer PostgreSQL integration:
1. Check this documentation
2. Run `./test_postgres_connection.sh` for diagnostics
3. Run `./verify_postgres.sh` for database health check
4. If the PostgreSQL server is down, contact:
   - **Email**: anne_young@berkeley.edu
   - **System Admins**: bwrc-sysadmins@lists.eecs.berkeley.edu

---

**Author**: Lawrence Rhee  
**Date**: April 1, 2026  
**Airflow Version**: 3.1.0  
**uv Version**: 0.10.8+  
**PostgreSQL Version**: 13.22 (on server)  
**Python Version**: 3.11.9
