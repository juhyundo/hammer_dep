#!/usr/bin/env bash
# Demo: cross-user permissions for the SledgeHammer Studio cache.
#
# Spins up a throwaway local Postgres, creates a few test users including
# 'colin', and walks Andre's permission test end to end:
#   Lawrence pushes -> Colin reads (denied) -> grant Colin -> reads succeed
#   -> revoke Colin -> reads denied again.
#
# Does not touch barney. Run any time. ~30 seconds total.

set -u  # error on unset variables, but DON'T set -e (we expect some commands to fail intentionally)

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
PGDATA=/tmp/pg-sledgehammer-demo/data
PGSOCK=/tmp/pg-sledgehammer-demo/sock
PGPORT=5544

cleanup() {
    pg_ctl -D "$PGDATA" stop -m fast 2>/dev/null || true
    rm -rf /tmp/pg-sledgehammer-demo
}
trap cleanup EXIT

# Use conda's Postgres if available, fall back to system Postgres
PG=${PG:-postgres}
INITDB=${INITDB:-initdb}
PGCTL=${PGCTL:-pg_ctl}
PSQL=${PSQL:-psql}

# Make sure all the binaries we need are reachable
for bin in "$INITDB" "$PGCTL" "$PSQL"; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "FAIL: $bin not on PATH. Install Postgres (conda install postgresql) and rerun." >&2
        exit 2
    fi
done

echo "=== Bootstrapping local Postgres sandbox at port $PGPORT ==="
rm -rf /tmp/pg-sledgehammer-demo
mkdir -p "$PGDATA" "$PGSOCK"
"$INITDB" -D "$PGDATA" -U postgres --auth-local=trust --auth-host=trust >/tmp/pg-sledgehammer-demo/initdb.log 2>&1
"$PGCTL" -D "$PGDATA" -l /tmp/pg-sledgehammer-demo/server.log \
         -o "-k $PGSOCK -p $PGPORT -h 127.0.0.1" start >/dev/null

PGURL="postgresql://postgres@127.0.0.1:$PGPORT/postgres"

echo "=== Creating sledgehammer_users group and three test logins ==="
"$PSQL" "$PGURL" <<SQL >/dev/null
CREATE ROLE sledgehammer_users NOLOGIN;
CREATE ROLE lawrencejrhee LOGIN PASSWORD 'demo';
CREATE ROLE colin         LOGIN PASSWORD 'demo';
CREATE ROLE juhyun        LOGIN PASSWORD 'demo';
SQL

# Point studio at the sandbox
export HAMMER_PG_HOST=127.0.0.1
export HAMMER_PG_PORT=$PGPORT
export HAMMER_PG_DB=postgres
export HAMMER_PG_USER=postgres
export HAMMER_PG_PASSWORD=" "
cd "$REPO_ROOT"

echo "=== Running studio init (creates schema, default-deny, group grants) ==="
.venv/bin/studio init

echo
echo "=== Step 1: Lawrence pushes a master_database for 'gcd' ==="
echo '{"design":"gcd","note":"test row, only Lawrence pushed this"}' > /tmp/demo_mdb.json
HAMMER_PG_USER=lawrencejrhee .venv/bin/studio grant lawrencejrhee 2>/dev/null || \
  .venv/bin/studio grant lawrencejrhee
HAMMER_PG_USER=lawrencejrhee .venv/bin/studio master-push gcd --master /tmp/demo_mdb.json

pass_fail() {
    local label=$1 expected=$2 actual=$3
    if [ "$expected" = "$actual" ]; then
        printf "  PASS: %s\n" "$label"
    else
        printf "  FAIL: %s (expected %s, got %s)\n" "$label" "$expected" "$actual"
    fi
}

try_read_as() {
    # Prints "ALLOWED" if the read succeeded, "DENIED" otherwise. Never errors out.
    local user=$1
    HAMMER_PG_USER="$user" .venv/bin/studio master-pull gcd >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo "ALLOWED"
    else
        echo "DENIED"
    fi
}

echo
echo "=== Step 2: Colin tries to read (NOT yet in group) — expect DENIED ==="
result=$(try_read_as colin)
pass_fail "Colin denied" "DENIED" "$result"

echo
echo "=== Step 3: Lawrence adds Colin to sledgehammer_users ==="
.venv/bin/studio grant colin

echo
echo "=== Step 4: Colin reads again — expect ALLOWED ==="
result=$(try_read_as colin)
pass_fail "Colin reads" "ALLOWED" "$result"

echo
echo "=== Step 5: Juhyun (NOT in group) tries to read — expect DENIED ==="
result=$(try_read_as juhyun)
pass_fail "Juhyun denied" "DENIED" "$result"

echo
echo "=== Step 6: Lawrence revokes Colin from sledgehammer_users ==="
.venv/bin/studio revoke colin

echo
echo "=== Step 7: Colin reads again — expect DENIED ==="
result=$(try_read_as colin)
pass_fail "Colin re-denied after revoke" "DENIED" "$result"

echo
echo "=== Demo complete. Sandbox will be torn down on exit. ==="
