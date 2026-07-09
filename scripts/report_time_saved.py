#!/usr/bin/env python3
"""Report how much wall-clock / CPU time the PD cache has saved.

Every PD-cache decision (HIT, MISS_STORE, SKIP_*) is recorded as an event with
the duration the tool run took. A HIT credits that duration as time saved,
because the stage was restored from cache instead of re-run. This script totals
those savings across many runs -- e.g. a whole tapeout or RTL bring-up -- so you
can see "the cache has saved us N hours so far".

Two sources of events:

  * the durable Postgres ledger (hammer_poc.pd_cache_events) -- complete; the
    cache layer appends to it on every event and it survives across runs.
  * the per-run JSONL files under $AIRFLOW_HOME/cache_events -- ephemeral; the
    DAG's exit_ task deletes each run's file after summarizing it, so this only
    has runs that haven't been cleaned up yet.

By default (`--source auto`) it reads the Postgres ledger and falls back to the
JSONL files if the DB is unreachable or empty. It never sums both unless you
pass `--source both` (which can double-count runs still present in both).

Run it under the SledgeHammer venv (so `hammer` imports), e.g.:

    source vlsi/hammer/venv.sh
    python vlsi/hammer/scripts/report_time_saved.py
    python vlsi/hammer/scripts/report_time_saved.py --group-by design --since 2026-06-01
    python vlsi/hammer/scripts/report_time_saved.py --dag RocketConfig --group-by stage

Equivalent to the `studio time-saved` subcommand.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import List, Optional

from hammer.vlsi import time_tracking


def _parse_when(s: Optional[str]) -> Optional[float]:
    """Parse a --since/--until value into epoch seconds.

    Accepts a raw epoch number, or YYYY-MM-DD, or 'YYYY-MM-DD HH:MM[:SS]'.
    """
    if not s:
        return None
    try:
        return float(s)  # already epoch seconds
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(datetime.strptime(s, fmt).timetuple())
        except ValueError:
            continue
    raise SystemExit(f"could not parse date/time {s!r}; use epoch, YYYY-MM-DD, "
                     f"or 'YYYY-MM-DD HH:MM'")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="report_time_saved.py",
        description="Total the PD cache's wall-clock / CPU time savings across runs.",
    )
    p.add_argument("--source", choices=["auto", "db", "jsonl", "both"], default="auto",
                   help="Where to read events from (default: auto = DB, fall back to JSONL).")
    p.add_argument("-g", "--group-by", default="stage",
                   choices=["stage", "dag", "design", "project", "module", "run", "none"],
                   help="Break the report down by this dimension (default: stage).")
    p.add_argument("--since", help="Only count events at/after this time (epoch or YYYY-MM-DD).")
    p.add_argument("--until", help="Only count events at/before this time (epoch or YYYY-MM-DD).")
    p.add_argument("--dag", help="Filter to dag_id containing this substring.")
    p.add_argument("--design", help="Filter to design containing this substring.")
    p.add_argument("--stage", help="Filter to stage containing this substring (e.g. synthesis, par).")
    p.add_argument("--user", help="Filter to triggering_user / owner containing this substring.")
    p.add_argument("--project", help="Filter to project containing this substring.")
    p.add_argument("--module", help="Filter to module containing this substring (hierarchical flows).")
    p.add_argument("--cache-only", action="store_true",
                   help="Count only cache-delivered savings (exclude dependency-check "
                        "skips, which a legacy make flow may also have skipped).")
    p.add_argument("--events-dir", help="Override the JSONL events dir "
                                        "(default: $AIRFLOW_HOME/cache_events).")
    p.add_argument("--limit", type=int, default=None, help="Max DB rows to read.")
    args = p.parse_args(argv)

    if args.source == "db" and args.events_dir:
        print("[warn] --events-dir is ignored with --source db.", file=sys.stderr)
    try:
        events, source = time_tracking.collect_savings_events(
            source=args.source,
            since=_parse_when(args.since), until=_parse_when(args.until),
            dag=args.dag, design=args.design, stage=args.stage, user=args.user,
            project=args.project, module=args.module, limit=args.limit,
            events_dir=args.events_dir,
        )
    except Exception as e:
        # only --source db surfaces a hard error here; auto/jsonl fall back
        raise SystemExit(f"Could not read cache events: {e}")
    if args.cache_only:
        events = time_tracking.exclude_depcheck_skips(events)
        source = f"{source}, cache-only"
    print(time_tracking.format_savings_report(events, group_by=args.group_by, source=source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
