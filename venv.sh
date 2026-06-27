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

# Real DB/SMTP secrets (incl. HAMMER_PG_PASSWORD) are GPG-encrypted in
# .sledgehammer/airflow-secrets.env.gpg; airflow.cfg ships a blank conn. Decrypt
# once and export into the shell so airflow and studio reach Postgres without a
# separate step. No-op once loaded; a cancelled or failed decrypt is non-fatal.
_sledge_load_secrets() {
    [ -n "${AIRFLOW__DATABASE__SQL_ALCHEMY_CONN:-}" ] && return 0
    local _enc="${AIRFLOW_HOME:-$(pwd)}/.sledgehammer/airflow-secrets.env.gpg"
    [ -f "$_enc" ] || return 0
    echo "[sledge] loading secrets (enter your GPG passphrase if asked) ..." >&2
    local _line _k _v
    while IFS= read -r _line; do
        case "$_line" in ''|\#*) continue ;; esac
        _line="${_line#export }"; _k="${_line%%=*}"; _v="${_line#*=}"
        _v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"
        export "$_k=$_v"
    done < <(gpg --quiet --no-symkey-cache --decrypt "$_enc" 2>/dev/null)
    [ -z "${AIRFLOW__DATABASE__SQL_ALCHEMY_CONN:-}" ] && \
        echo "[sledge] secrets not loaded (cancelled or wrong passphrase); rerun 'source venv.sh' to retry." >&2
    return 0
}

# Load them up front when a person sources this. Skipped without a tty (scripts,
# cron) so nothing blocks on a passphrase prompt; opt out with SLEDGE_NO_AUTO_SECRETS=1.
# The launcher skips its own decrypt when the conn is already set, so one prompt.
if [ -t 0 ] && [ -z "${SLEDGE_NO_AUTO_SECRETS:-}" ]; then
    _sledge_load_secrets
fi

# Bare `airflow` still loads them lazily in case the up-front step was skipped,
# then runs the real binary. Subprocesses use the real binary directly.
airflow() {
    _sledge_load_secrets
    command airflow "$@"
}
