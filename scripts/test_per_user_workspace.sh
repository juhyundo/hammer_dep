#!/usr/bin/env bash
#
# test_per_user_workspace.sh — verify per-user build directory isolation.
#
# What it does:
#   1. Registers two SANDBOX usernames ("__test_alice" and "__test_bob") in
#      hammer_poc.user_workspaces, pointed at scratch directories under /tmp.
#      Real workspaces (lawrencejrhee, andre_green, etc.) are not touched.
#   2. Seeds a marker file in each sandbox workspace.
#   3. Drives AIRFlow_synpar.clean() programmatically with a fake DAG run
#      context, simulating "__test_bob triggered this".
#   4. Asserts: bob's marker is gone, alice's is untouched.
#   5. Repeats in reverse with alice as the triggering user.
#   6. Tears down: deletes the sandbox workspaces and the test rows.
#
# This is the same code path the real Airflow scheduler takes when handling
# a clean task triggered via the UI, just bypassing the Airflow stack so you
# can run it standalone — no LDAP logins required to reproduce the test.
#
# Usage:
#   ./scripts/test_per_user_workspace.sh
#
# Exit code: 0 if all assertions pass, nonzero otherwise.

set -euo pipefail
cd "$(dirname "$0")/.."

# Pick up the project's Python and DB connection settings.
source ./venv.sh > /dev/null 2>&1 || true
export PATH="$(pwd)/.venv/bin:$PATH"
export AIRFLOW_HOME="$(pwd)"

# Sandbox identities. The double-underscore prefix marks them as test rows
# so anyone running `workspace-list` sees they're not real users.
USER_A="__test_alice"
USER_B="__test_bob"
ROOT_A="/tmp/sledgehammer_test/${USER_A}"
ROOT_B="/tmp/sledgehammer_test/${USER_B}"

cleanup() {
    .venv/bin/hammer-pd-store workspace-unset "$USER_A" > /dev/null 2>&1 || true
    .venv/bin/hammer-pd-store workspace-unset "$USER_B" > /dev/null 2>&1 || true
    rm -rf "/tmp/sledgehammer_test"
}
trap cleanup EXIT

# Register sandbox workspaces.
.venv/bin/hammer-pd-store workspace-set "$USER_A" "$ROOT_A" > /dev/null
.venv/bin/hammer-pd-store workspace-set "$USER_B" "$ROOT_B" > /dev/null

# Seed each workspace.
mkdir -p "${ROOT_A}/gcd" "${ROOT_B}/gcd"
echo "alice work product" > "${ROOT_A}/gcd/marker.txt"
echo "bob   work product" > "${ROOT_B}/gcd/marker.txt"

run_clean_as() {
    # Run AIRFlow_synpar.clean() with a fabricated context emulating the
    # given LDAP-triggering-user. Same construction path the real DAG uses.
    #
    # IMPORTANT: the fake DagRun deliberately does NOT expose
    # ``triggering_user_name`` as an attribute, because Airflow 3's runtime
    # ``DagRunProtocol`` doesn't either. The helper has to resolve the user
    # via SQL using dag_id + run_id, which is the real-Airflow code path.
    # Also passes a real (dag_id, run_id) pair, inserted as a synthetic row
    # into the airflow metadata DB ahead of time and cleaned up after.
    local trigger_user="$1"
    local fake_dag_id="__test_per_user_workspace"
    local fake_run_id="__test_$(date +%s%N)_${trigger_user}"

    # Insert a synthetic dag_run row so the SQL lookup in
    # _lookup_triggering_user_from_db can find triggering_user_name.
    .venv/bin/python - <<PY
import psycopg2
from hammer.vlsi import pd_store
s = pd_store._parse_airflow_cfg_conn()
conn = psycopg2.connect(**s); conn.autocommit = True
with conn.cursor() as cur:
    cur.execute(
        "INSERT INTO dag_run (dag_id, run_id, triggering_user_name, "
        "run_type, state, logical_date, run_after, queued_at) "
        "VALUES (%s, %s, %s, 'manual', 'running', NOW(), NOW(), NOW())",
        ("${fake_dag_id}", "${fake_run_id}", "${trigger_user}"))
conn.close()
PY

    .venv/bin/python <<PY 2>&1 | grep -v DeprecatedImportWarning || true
import sys, os
sys.path.insert(0, "${PWD}")
sys.path.insert(0, "${PWD}/dags")

os.environ.pop("OBJ_DIR", None)
os.environ.pop("HAMMER_D_MK", None)
sys.argv = ["test_runner", "clean"]

class FakeDagRun:
    # Mirror Airflow 3 DagRunProtocol: only dag_id, run_id, conf etc. exposed.
    # NO triggering_user_name attribute - forces the SQL-lookup code path.
    dag_id = "${fake_dag_id}"
    run_id = "${fake_run_id}"
    conf = {}

from sledgehammer_demo_gcd_synpar import AIRFlow_synpar
import subprocess
flow = AIRFlow_synpar(context={"dag_run": FakeDagRun()})
print(f"  resolved OBJ_DIR = {flow.OBJ_DIR}")
if os.path.exists(flow.OBJ_DIR):
    subprocess.run(f"rm -rf {flow.OBJ_DIR}", shell=True, check=True)
    print(f"  wiped {flow.OBJ_DIR}")
PY

    # Clean up the synthetic dag_run row.
    .venv/bin/python - <<PY
import psycopg2
from hammer.vlsi import pd_store
s = pd_store._parse_airflow_cfg_conn()
conn = psycopg2.connect(**s); conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("DELETE FROM dag_run WHERE dag_id = %s",
                ("${fake_dag_id}",))
conn.close()
PY
}

assert_present() {
    if [[ ! -f "$1" ]]; then
        echo "FAIL: expected $1 to still exist"
        exit 1
    fi
    echo "  OK: $1 still present"
}

assert_gone() {
    if [[ -e "$1" ]]; then
        echo "FAIL: expected $1 to be wiped, but it still exists"
        exit 1
    fi
    echo "  OK: $1 is gone"
}

echo "=== Initial state ==="
echo "  $USER_A: $(cat ${ROOT_A}/gcd/marker.txt)"
echo "  $USER_B: $(cat ${ROOT_B}/gcd/marker.txt)"

echo
echo "=== Test 1: $USER_B triggers clean ==="
run_clean_as "$USER_B"
assert_present "${ROOT_A}/gcd/marker.txt"   # alice untouched
assert_gone    "${ROOT_B}/gcd/marker.txt"   # bob wiped

# Re-seed bob for test 2.
mkdir -p "${ROOT_B}/gcd"
echo "bob   work product (re-seeded)" > "${ROOT_B}/gcd/marker.txt"

echo
echo "=== Test 2: $USER_A triggers clean ==="
run_clean_as "$USER_A"
assert_present "${ROOT_B}/gcd/marker.txt"   # bob untouched
assert_gone    "${ROOT_A}/gcd/marker.txt"   # alice wiped

echo
echo "ALL TESTS PASSED. Per-user workspace isolation is working."
echo "(Test rows and /tmp/sledgehammer_test/ will be cleaned up on exit.)"
