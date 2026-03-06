#!/usr/bin/env python3
"""AutoTA+ Pruner — retry limiter and branch status tracker.

Prevents infinite branch loops and provides a report of all branches.
"""
import os
import json
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BRANCHES_PATH = os.path.join(SCRIPT_DIR, "branches.json")

MAX_RETRIES = 3  # max branches from the same origin phase


def get_retry_count(phase: str) -> int:
    """Count how many branches have been spawned from this phase."""
    if not os.path.exists(BRANCHES_PATH):
        return 0
    try:
        with open(BRANCHES_PATH, "r") as f:
            data = json.load(f)
        return sum(1 for b in data.get("branches", [])
                   if b.get("phase_origin") == phase
                   and b.get("status") in ("triggered", "running"))
    except Exception:
        return 0


def too_many_retries(phase: str) -> bool:
    """Check if we've hit the retry limit for this phase."""
    count = get_retry_count(phase)
    return count >= MAX_RETRIES


def mark_branch_status(branch_id: str, status: str, reason: str = ""):
    """Update a branch's status in branches.json."""
    if not os.path.exists(BRANCHES_PATH):
        return
    try:
        with open(BRANCHES_PATH, "r") as f:
            data = json.load(f)
        for b in data.get("branches", []):
            if b.get("branch_id") == branch_id:
                b["status"] = status
                b["status_reason"] = reason
                b["status_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                break
        with open(BRANCHES_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def prune_report() -> str:
    """One-line summary of active branches."""
    if not os.path.exists(BRANCHES_PATH):
        return ""
    try:
        with open(BRANCHES_PATH, "r") as f:
            data = json.load(f)
    except Exception:
        return ""

    branches = data.get("branches", [])
    active = [b for b in branches if b.get("status") in ("triggered", "running")]
    if not active:
        return ""
    lines = [f"\n=== ACTIVE BRANCHES: {len(active)} ==="]
    for b in active:
        lines.append(f"  {b['branch_id']}  [{b.get('phase_origin','?')}]  {b.get('status','?')}")
    return "\n".join(lines)
