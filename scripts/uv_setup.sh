#!/usr/bin/env bash
# From-scratch environment setup for Hammer + Airflow + Postgres under uv.
# Run from a fresh clone:  ./scripts/uv_setup.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PG_LOCAL="$HOME/pg_local"
LIBNSL_LOCAL="$HOME/libnsl_local"
SECRETS_DIR="$HOME/.config/sledgehammer"
SECRETS_FILE="${SLEDGE_SECRETS_FILE:-$SECRETS_DIR/airflow-secrets.env.gpg}"
AIRFLOW_VERSION="${AIRFLOW_VERSION:-3.1.0}"
PYVER="${PYVER:-3.11}"

step() { printf '\n=== %s ===\n' "$1"; }

step "uv"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version

step "pg_config (psycopg2 builds from source)"
if [ ! -x "$PG_LOCAL/usr/bin/pg_config" ]; then
    tmp="$(mktemp -d)"
    ( cd "$tmp"
      dnf download libpq-devel
      dnf download libpq
      mkdir -p "$PG_LOCAL"
      rpm2cpio libpq-devel-*x86_64.rpm | ( cd "$PG_LOCAL" && cpio -idmv )
      rpm2cpio libpq-[0-9]*x86_64.rpm  | ( cd "$PG_LOCAL" && cpio -idmv ) )
    ln -sf libpq.so.5 "$PG_LOCAL/usr/lib64/libpq.so"
    rm -rf "$tmp"
fi
export PATH="$PG_LOCAL/usr/bin:$PATH"
pg_config --version

step "libnsl (Cadence tools on RHEL 9)"
if [ ! -f "$LIBNSL_LOCAL/usr/lib64/libnsl.so.1" ]; then
    tmp="$(mktemp -d)"
    ( cd "$tmp"
      dnf download libnsl
      mkdir -p "$LIBNSL_LOCAL"
      rpm2cpio libnsl-*x86_64.rpm | ( cd "$LIBNSL_LOCAL" && cpio -idmv ) )
    rm -rf "$tmp"
fi
ls "$LIBNSL_LOCAL/usr/lib64/libnsl.so.1"

step "persist PATH in ~/.bashrc"
grep -q 'pg_local/usr/bin' "$HOME/.bashrc" 2>/dev/null || \
    printf '\nexport PATH="$HOME/.local/bin:$HOME/pg_local/usr/bin:$PATH"\n' >> "$HOME/.bashrc"

step "virtual environment + dependencies"
uv python install "$PYVER"
[ -d .venv ] || uv venv --python "$PYVER"
uv lock
uv sync --group dev

step "airflow $AIRFLOW_VERSION"
source .venv/bin/activate
PYTHON_VERSION="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
CONSTRAINT="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"
uv pip uninstall myst-parser mdit-py-plugins markdown-it-py >/dev/null 2>&1 || true
uv pip install "apache-airflow==${AIRFLOW_VERSION}" apache-airflow-providers-fab --constraint "$CONSTRAINT"
uv pip install "psycopg2==2.9.11" --no-binary psycopg2 --reinstall
AIRFLOW_HOME="$(mktemp -d)" airflow version

step "secrets (committed airflow.cfg ships blank; create the encrypted env)"
if [ -f "$SECRETS_FILE" ]; then
    echo "already present: $SECRETS_FILE"
else
    read -rp "  set up Postgres secrets now? [Y/n]: " DO_SECRETS
    if [ "${DO_SECRETS,,}" = "n" ]; then
        echo "  skipped -- Airflow won't start until these exist."
        echo "  create them later: ./scripts/sledge-secrets-create.sh"
    else
        "$REPO/scripts/sledge-secrets-create.sh"
    fi
fi

step "done"
cat <<EOF
Setup complete. Start Airflow with:
    source ./venv.sh && export PATH="\$(pwd)/.venv/bin:\$PATH"
    ./scripts/airflow-standalone-ldap.py
(first launch runs the DB migrations automatically.)
EOF
