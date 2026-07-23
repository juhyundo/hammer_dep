#!/usr/bin/env bash
# From-scratch environment setup for Hammer + Airflow + Postgres under uv.
# Run from a fresh clone:  ./scripts/uv_setup.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Sanitize the build environment. psycopg2 and python-ldap compile from source
# here; whatever OpenSSL / libpq is on the linker path gets baked into the
# binaries as an RPATH, and a FOREIGN one (conda's, an active venv's, an env
# module's) then breaks the binary in every future shell -- e.g. conda's
# libcrypto lacks EVP_md2, so psycopg2 fails to import forever after. No later
# cleanup fixes an RPATH. So we neutralize the known offenders before building
# (uv builds the venv with its own standalone Python, so the result is clean),
# and -- as a catch-all for anything we did not anticipate -- verify at the end
# that psycopg2 actually imports.
_strip_pathlike() {   # $1 = the ':'-list; $2.. = glob patterns of entries to drop
    local list="$1"; shift
    local out="" p pat drop; local IFS=:
    for p in $list; do
        drop=0
        for pat in "$@"; do case "$p" in $pat) drop=1; break ;; esac; done
        [ "$drop" = 0 ] && out="${out:+$out:}$p"
    done
    printf '%s' "$out"
}
_neutralized=""
# 1. conda / mamba / micromamba / miniforge / pixi -- the usual culprit
if [ -n "${CONDA_PREFIX:-}${CONDA_DEFAULT_ENV:-}${MAMBA_ROOT_PREFIX:-}${PIXI_PROJECT_ROOT:-}" ] \
   || printf '%s' "${PATH:-}" | grep -qiE 'conda|miniforge|mamba|pixi'; then
    _neutralized="$_neutralized conda/mamba/pixi"
    PATH="$(_strip_pathlike "$PATH" '*conda*' '*miniforge*' '*mamba*' '*pixi*')"; export PATH
    [ -n "${LD_LIBRARY_PATH:-}" ] && { LD_LIBRARY_PATH="$(_strip_pathlike "$LD_LIBRARY_PATH" '*conda*' '*miniforge*' '*mamba*' '*pixi*')"; export LD_LIBRARY_PATH; }
    unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER \
          CONDA_EXE CONDA_PYTHON_EXE MAMBA_ROOT_PREFIX PIXI_PROJECT_ROOT 2>/dev/null || true
fi
# 2. an already-active pip/uv virtualenv would shadow the one we are about to build
if [ -n "${VIRTUAL_ENV:-}" ]; then
    _neutralized="$_neutralized venv($(basename "$VIRTUAL_ENV"))"
    PATH="$(_strip_pathlike "$PATH" "$VIRTUAL_ENV/bin")"; export PATH
    unset VIRTUAL_ENV 2>/dev/null || true
fi
# 3. an LD_PRELOAD injects a library into every build subprocess
[ -n "${LD_PRELOAD:-}" ] && { _neutralized="$_neutralized LD_PRELOAD"; unset LD_PRELOAD; }
if [ -n "$_neutralized" ]; then
    echo "note: neutralized for a clean build:$_neutralized"
fi
# 4. anything left on LD_LIBRARY_PATH that is not a system path (env modules,
#    spack, a hand-set lib dir) can still carry a foreign OpenSSL. We do not
#    strip it blindly (it may be intentional), but flag it so a later failure
#    has an obvious first thing to try.
if [ -n "${LD_LIBRARY_PATH:-}" ] \
   && printf '%s' "$LD_LIBRARY_PATH" | tr ':' '\n' | grep -vqE '^(/usr/|/lib|/opt/dell|$)'; then
    echo "note: LD_LIBRARY_PATH has non-system entries below; if the build's psycopg2"
    echo "      check fails, 'unset LD_LIBRARY_PATH' and rerun:"
    printf '        %s\n' "$LD_LIBRARY_PATH"
fi

PG_LOCAL="$HOME/pg_local"
LIBNSL_LOCAL="$HOME/libnsl_local"
LDAP_LOCAL="$HOME/ldap_local"
SECRETS_DIR="$REPO/.sledgehammer"
SECRETS_FILE="${SLEDGE_SECRETS_FILE:-$SECRETS_DIR/airflow-secrets.env.gpg}"
AIRFLOW_VERSION="${AIRFLOW_VERSION:-3.1.0}"
FAB_VERSION="${FAB_VERSION:-3.6.3}"
EDGE3_VERSION="${EDGE3_VERSION:-1.3.0}"
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
# Edge worker support (constrained so it can't drag airflow to a newer release).
# Installed BEFORE fab: fab is deliberately unconstrained and must resolve last
# so the newer deps it needs (pyjwt, common-compat, sqlalchemy) end up on top.
uv pip install "apache-airflow-providers-edge3==${EDGE3_VERSION}" --constraint "$CONSTRAINT"
uv pip install "apache-airflow-providers-fab==${FAB_VERSION}"
CPPFLAGS="-I$LDAP_LOCAL/usr/include -I/usr/include ${CPPFLAGS:-}" \
LDFLAGS="-L$LDAP_LOCAL/usr/lib64 -L/lib64 ${LDFLAGS:-}" \
    uv pip install "python-ldap==${LDAP_VERSION}" --no-binary python-ldap
uv pip install "psycopg2==2.9.11" --no-binary psycopg2 --reinstall
AIRFLOW_HOME="$(mktemp -d)" airflow version

step "verify the compiled build is clean (no foreign library baked in)"
# The real safety net: whatever environment we failed to strip above, a
# tainted psycopg2 shows up here as an import error. Fail loudly with the fix
# instead of leaving a broken venv that only breaks later, at first DB use.
if python3 -c "import psycopg2" 2>/tmp/_pg_err; then
    echo "  psycopg2 imports clean"
else
    echo "ERROR: psycopg2 was built against a foreign library and cannot load:" >&2
    sed 's/^/  /' /tmp/_pg_err >&2
    echo "  This means an environment was active that put a foreign OpenSSL/libpq" >&2
    echo "  on the linker path. Check for a conda/venv/module/spack environment or" >&2
    echo "  a non-system LD_LIBRARY_PATH, clear it, and rerun this script." >&2
    rm -f /tmp/_pg_err
    exit 1
fi
rm -f /tmp/_pg_err

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
