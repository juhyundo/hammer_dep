"""Demo: compute_stage_key correctness against a real master_database."""

import copy
import json
import os
import sys

from hammer.vlsi import pd_store

MASTER_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "e2e", "master_database.json")


def short(h: str) -> str:
    return h[:16] + "..."


def main() -> int:
    db = json.load(open(MASTER_PATH))
    print(f"master_database: {MASTER_PATH}")
    print(f"total keys: {len(db)}")
    print()

    print("Per-stage cache keys and slice sizes")
    print("-" * 60)
    for stage in ("synthesis", "par", "drc", "lvs"):
        sliced = pd_store._stage_relevant_keys(db, stage)
        sha = pd_store.compute_stage_key(db, stage)
        print(f"{stage:<14} keys_in_slice={len(sliced):<4} sha={short(sha)}")
    print()

    syn_key_a = pd_store.compute_stage_key(db, "synthesis")
    syn_key_b = pd_store.compute_stage_key(db, "synthesis")
    print("[1] STABILITY: same input -> same hash")
    print(f"    call 1: {short(syn_key_a)}")
    print(f"    call 2: {short(syn_key_b)}")
    print(f"    equal? {syn_key_a == syn_key_b}")
    print()

    mutated = copy.deepcopy(db)
    mutated["synthesis.inputs.input_files"] = ["/foo/bar.v"]
    syn_orig = pd_store.compute_stage_key(db, "synthesis")
    syn_mut = pd_store.compute_stage_key(mutated, "synthesis")
    print("[2] SENSITIVITY: change syn input -> syn hash changes")
    print(f"    original syn: {short(syn_orig)}")
    print(f"    mutated  syn: {short(syn_mut)}")
    print(f"    different? {syn_orig != syn_mut}")
    print()

    par_orig = pd_store.compute_stage_key(db, "par")
    par_mut = pd_store.compute_stage_key(mutated, "par")
    print("[3] SELECTIVITY: change syn input -> par hash unchanged")
    print(f"    original par: {short(par_orig)}")
    print(f"    mutated  par: {short(par_mut)}")
    print(f"    equal? {par_orig == par_mut}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
