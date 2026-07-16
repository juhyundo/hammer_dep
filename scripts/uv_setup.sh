#!/usr/bin/env bash
# From-scratch environment setup for Hammer + Airflow + Postgres under uv.
# Run from a fresh clone:  ./scripts/uv_setup.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Refuse to build under conda. psycopg2 and python-ldap compile from source
# here, and an active conda env bakes its library path into the binaries as
# an RPATH; the resulting psycopg2 then loads conda's OpenSSL forever after,
# in every shell, and fails with "undefined symbol: EVP_md2". No environment
# cleanup can fix an RPATH: the only cure is rebuilding, so don't let the
# tainted build happen in the first place.
if [ -n "${CONDA_PREFIX:-}" ] || [ -n "${CONDA_DEFAULT_ENV:-}" ]; then
    echo "ERROR: a conda environment is active (${CONDA_PREFIX:-$CONDA_DEFAULT_ENV})." >&2
    echo "Building under conda bakes conda library paths into compiled packages." >&2
    echo "Run 'conda deactivate' until no env is active (including base), open a" >&2
    echo "fresh login shell, and rerun this script." >&2
    exit 1
fi

PG_LOCAL="$HOME/pg_local"
LIBNSL_LOCAL="$HOME/libnsl_local"
LDAP_LOCAL="$HOME/ldap_local"
SECRETS_DIR="$REPO/.sledgehammer"
SECRETS_FILE="${SLEDGE_SECRETS_FILE:-$SECRETS_DIR/airflow-secrets.env.gpg}"
AIRFLOW_VERSION="${AIRFLOW_VERSION:-3.1.0}"
FAB_VERSION="${FAB_VERSION:-3.6.3}"
LDAP_VERSION="${LDAP_VERSION:-3.4.7}"
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

step "OpenLDAP headers (python-ldap builds from source)"
if [ ! -f "$LDAP_LOCAL/usr/include/lber.h" ]; then
    tmp="$(mktemp -d)"
    ( cd "$tmp"
      dnf download openldap-devel
      mkdir -p "$LDAP_LOCAL"
      rpm2cpio openldap-devel-*x86_64.rpm | ( cd "$LDAP_LOCAL" && cpio -idmv ) )
    # openldap-devel ships libldap.so/liblber.so symlinks to .so.2 files that
    # live in the runtime 'openldap' package (already in /lib64); repoint them
    # so the linker resolves -lldap/-llber against the system libs.
    ln -sf /lib64/libldap.so.2 "$LDAP_LOCAL/usr/lib64/libldap.so"
    ln -sf /lib64/liblber.so.2 "$LDAP_LOCAL/usr/lib64/liblber.so"
    rm -rf "$tmp"
fi
ls "$LDAP_LOCAL/usr/include/lber.h"

step "persist PATH in ~/.bashrc"
grep -q 'pg_local/usr/bin' "$HOME/.bashrc" 2>/dev/null || \
    printf '\nexport PATH="$HOME/.local/bin:$HOME/pg_local/usr/bin:$PATH"\n' >> "$HOME/.bashrc"

step "install 'sledgehammer' launch command in ~/.bashrc"
grep -q 'sledgehammer()' "$HOME/.bashrc" 2>/dev/null || cat >> "$HOME/.bashrc" <<'EOF'

sledgehammer() {
    local repo; repo="$(git rev-parse --show-toplevel 2>/dev/null)"
    [ -n "$repo" ] && [ -f "$repo/scripts/airflow-standalone-ldap.py" ] || { echo "sledgehammer: cd into a hammer checkout first"; return 1; }
    ( cd "$repo" && source ./venv.sh && export PATH="$repo/.venv/bin:$PATH" && exec ./scripts/airflow-standalone-ldap.py "$@" )
}
EOF

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
uv pip install "apache-airflow==${AIRFLOW_VERSION}" --constraint "$CONSTRAINT"
uv pip install "apache-airflow-providers-fab==${FAB_VERSION}"
CPPFLAGS="-I$LDAP_LOCAL/usr/include -I/usr/include ${CPPFLAGS:-}" \
LDFLAGS="-L$LDAP_LOCAL/usr/lib64 -L/lib64 ${LDFLAGS:-}" \
    uv pip install "python-ldap==${LDAP_VERSION}" --no-binary python-ldap
uv pip install "psycopg2==2.9.11" --no-binary psycopg2 --reinstall
AIRFLOW_HOME="$(mktemp -d)" airflow version

step "hammer plugins (editable, any that sit next to this checkout)"
# Tech/PDK plugins (tstech*, mentor, etc.) are separate packages, not deps of
# hammer-vlsi, so `uv sync` never installs them. When this checkout lives
# inside a design tree (e.g. chipyard/vlsi/hammer), the plugins are siblings:
# install any that are present so a chipyard integration is ready without a
# manual pip step. A standalone hammer clone simply finds none. Skip with
# SLEDGE_NO_PLUGINS=1.
if [ -z "${SLEDGE_NO_PLUGINS:-}" ]; then
    shopt -s nullglob
    _found_plugin=0
    for _plug in "$(dirname "$REPO")"/hammer-*-plugin*; do
        [ -f "$_plug/pyproject.toml" ] || [ -f "$_plug/setup.py" ] || continue
        echo "  installing $(basename "$_plug")"
        uv pip install -e "$_plug"
        _found_plugin=1
    done
    shopt -u nullglob
    [ "$_found_plugin" = 0 ] && echo "  none found next to $(dirname "$REPO") (standalone checkout; nothing to do)"
else
    echo "  skipped (SLEDGE_NO_PLUGINS set)"
fi

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
