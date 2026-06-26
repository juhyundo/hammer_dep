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

# Make bare `airflow` commands work without a separate wrapper. airflow.cfg ships
# with a blank DB connection (the real one is GPG-encrypted), so a plain airflow
# command can't reach the metadata DB. This shadows `airflow` with a function
# that loads the secrets into the shell on first use, then runs the real binary.
# Decrypted once per shell (later calls reuse the environment); no passphrase
# when you source this file, only the first time you actually run airflow.
# Subprocesses still use the real binary, so this only affects interactive use.
airflow() {
    if [ -z "${AIRFLOW__DATABASE__SQL_ALCHEMY_CONN:-}" ]; then
        local _enc="${AIRFLOW_HOME:-$(pwd)}/.sledgehammer/airflow-secrets.env.gpg"
        if [ -f "$_enc" ]; then
            echo "[sledge] loading secrets for airflow (enter your GPG passphrase if asked) ..." >&2
            local _line _k _v
            while IFS= read -r _line; do
                case "$_line" in ''|\#*) continue ;; esac
                _line="${_line#export }"; _k="${_line%%=*}"; _v="${_line#*=}"
                _v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"
                export "$_k=$_v"
            done < <(gpg --quiet --decrypt "$_enc" 2>/dev/null)
            [ -z "${AIRFLOW__DATABASE__SQL_ALCHEMY_CONN:-}" ] && \
                echo "[sledge] WARNING: secrets didn't load (wrong passphrase?); airflow may error on a blank DB conn." >&2
        fi
    fi
    command airflow "$@"
}
