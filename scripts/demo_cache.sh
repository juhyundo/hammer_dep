#!/usr/bin/env bash
# Demo the PD cache end to end on a real syn run.
# Run from the hammer repo root on a machine where Genus is on PATH.
#
# Usage:
#   ./scripts/demo_cache.sh
#
# Walks four runs: cold (MISS), warm (HIT), mutated config (MISS),
# reverted config (HIT). Prints just the cache lines from each.

set -e

DESIGN=${DESIGN:-gcd}
PDK=${PDK:-sky130}
TOOLS=${TOOLS:-cm}
ENV_NAME=${ENV_NAME:-bwrc}
OBJ_DIR=e2e/build-${PDK}-${TOOLS}/${DESIGN}
LOG=/tmp/demo_cache.log

if ! command -v genus >/dev/null 2>&1; then
    echo "genus not on PATH. Source your Cadence env, then retry." >&2
    exit 1
fi

# shellcheck disable=SC1091
source ./venv.sh

export HAMMER_PD_CACHE=1

run_syn() {
    local tag=$1
    echo
    echo "================================================================"
    echo "[$tag] running: make syn design=$DESIGN pdk=$PDK tools=$TOOLS"
    echo "================================================================"
    rm -rf "$OBJ_DIR/syn-rundir"
    (cd e2e && make syn design=$DESIGN pdk=$PDK tools=$TOOLS env=$ENV_NAME) 2>&1 | tee "$LOG" | grep --line-buffered "PD cache" || true
}

# Cold start: full wipe.
rm -rf "$OBJ_DIR"
run_syn "Run 1 / cold (expect MISS + STORE)"

# Same inputs.
run_syn "Run 2 / warm (expect HIT)"

# Mutate clock period to invalidate the cache.
sed -i.bak 's/period: "20.0ns"/period: "15.0ns"/' e2e/configs-design/$DESIGN/common.yml
run_syn "Run 3 / mutated config (expect MISS + STORE)"

# Revert the mutation.
mv e2e/configs-design/$DESIGN/common.yml.bak e2e/configs-design/$DESIGN/common.yml
run_syn "Run 4 / reverted config (expect HIT)"

echo
echo "Full last-run log: $LOG"
