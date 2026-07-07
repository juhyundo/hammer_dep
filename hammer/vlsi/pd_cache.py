"""
Postgres-backed cache wrapper for Hammer's per-stage actions.

`cache_or_run` wraps a stage's run callable (driver.run_synthesis,
driver.run_par, ...) so that an identical run anywhere else on the team
restores from a stored tarball instead of running the tool again.

Cache key is the stage's slice of the live driver config, plus a content
hash over the RTL files. Hit means we untar the stored rundir and read
<stage>-output.json back out. Miss means we run the tool normally and
push the resulting rundir for the next person.

Off by default. Set HAMMER_PD_CACHE=1 (or vlsi.pd_cache.enabled in a
config) to turn it on. If anything goes wrong (DB down, missing file,
broken tarball) we log a warning and just run the stage like usual.

Access control is whatever Postgres GRANTs the connecting role. If the
user can SELECT from pd_blobs they can read the cache; if they can't,
load_stage_blob returns None and the stage runs from scratch.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    # POSIX-only; harmless to skip on Windows since the cache is Linux-only.
    import resource as _resource
except ImportError:  # pragma: no cover
    _resource = None  # type: ignore

from hammer.vlsi import pd_store


def _child_cpu_seconds() -> Optional[float]:
    """
    Cumulative CPU (user + sys) consumed by all child processes waited on so
    far in this process. Take a snapshot before and after run_fn() and
    subtract to get the CPU time the tool itself burned.

    Returns None if the resource module isn't available.
    """
    if _resource is None:
        return None
    ru = _resource.getrusage(_resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


def _format_duration(seconds: float) -> str:
    """Pretty-print a wall-clock duration. Short units for short durations."""
    if seconds < 1.0:
        return f"{seconds*1000:.0f}ms"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    if seconds < 3600.0:
        m, s = divmod(int(round(seconds)), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(round(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _format_savings(
    original_duration: Optional[float],
    restore_seconds: float,
    original_cpu: Optional[float] = None,
) -> str:
    """Build the '(saved ~Xs vs original Ys tool run)' suffix, or '' if unknown.

    If ``original_cpu`` is set, also report the CPU time saved (which for
    multi-threaded tools like Innovus is often much bigger than wall-clock
    saved).
    """
    if original_duration is None or original_duration <= 0:
        return ""
    saved = max(0.0, original_duration - restore_seconds)
    msg = (
        f" Saved ~{_format_duration(saved)} wall "
        f"(restore {_format_duration(restore_seconds)} "
        f"vs original {_format_duration(original_duration)} tool run)"
    )
    if original_cpu is not None and original_cpu > 0:
        # Restore is single-threaded tar untar, so CPU cost is ~= wall cost
        # for that side. Subtract it from the cached CPU figure.
        cpu_saved = max(0.0, original_cpu - restore_seconds)
        msg += (
            f"; ~{_format_duration(cpu_saved)} CPU "
            f"(original tool consumed {_format_duration(original_cpu)} CPU)"
        )
    return msg + "."


# ----- Per-DAG-run event log (for the exit_ task summary) -----

def _events_dir() -> Optional[Path]:
    """Where to write per-run cache event JSONL files."""
    airflow_home = os.environ.get("AIRFLOW_HOME")
    if not airflow_home:
        return None
    p = Path(airflow_home) / "cache_events"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return p


def _events_file_for_run(run_id: str) -> Optional[Path]:
    d = _events_dir()
    if d is None or not run_id:
        return None
    # Sanitize run_id for filename use (slashes, colons, plus signs from
    # Airflow's manual__<isotimestamp>+00:00 format would otherwise break).
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in run_id)
    return d / f"{safe}.jsonl"


def _record_cache_event(
    stage_tag: str,
    outcome: str,
    *,
    saved_seconds: Optional[float] = None,
    tool_seconds: Optional[float] = None,
    restore_seconds: Optional[float] = None,
    saved_cpu_seconds: Optional[float] = None,
    tool_cpu_seconds: Optional[float] = None,
    enabled: Optional[bool] = None,
) -> None:
    """
    Append a single cache event for the current DAG run.

    Outcomes: ``HIT``, ``MISS_STORE``, ``SKIP_LOCAL`` (dep-check skip,
    local files were already on disk), ``SKIP_RESTORED`` (dep-check skip,
    files missing so restored from tarball), ``SKIP_NO_BLOB`` (dep-check
    skip, no local files AND no cache blob - downstream will likely fail).

    The ``*_cpu_seconds`` pair mirrors the wall-clock pair: for MISS_STORE
    we record what the tool just burned; for HIT/SKIP we record the saved
    figure pulled from the cached blob.

    Silently no-ops outside an Airflow run (no AIRFLOW_HOME or no run_id).
    """
    run_id = os.environ.get("HAMMER_AIRFLOW_RUN_ID", "")
    dag_id = os.environ.get("HAMMER_AIRFLOW_DAG_ID", "")
    design = os.environ.get("HAMMER_AIRFLOW_DESIGN") or os.environ.get("design") or None
    project = os.environ.get("HAMMER_PD_PROJECT") or None
    # 1. Per-run JSONL log -- feeds the exit_ task's one-run summary, then gets
    #    deleted by clear_run_cache_events. Only written inside an Airflow run.
    #    Carries design/project so the exit_ summary can show which project
    #    this run was tagged under.
    f = _events_file_for_run(run_id)
    if f is not None:
        event = {
            "ts":                time.time(),
            "dag_id":            dag_id,
            "run_id":            run_id,
            "stage_tag":         stage_tag,
            "outcome":           outcome,
            "saved_seconds":     saved_seconds,
            "tool_seconds":      tool_seconds,
            "restore_seconds":   restore_seconds,
            "saved_cpu_seconds": saved_cpu_seconds,
            "tool_cpu_seconds":  tool_cpu_seconds,
            "design":            design,
            "project":           project,
        }
        try:
            with f.open("a") as fh:
                fh.write(json.dumps(event) + "\n")
        except Exception:
            # Telemetry must never fail the run.
            pass
    # 2. Durable Postgres ledger -- survives the exit_ cleanup so the time-saved
    #    tracker can total savings across every run of a tapeout. Gated by the
    #    ledger switch (default on; HAMMER_PD_CACHE_LEDGER=0 to disable). Best
    #    effort only: the DB may be unreachable, or a direct shell run may have
    #    no Postgres password, so any failure here is swallowed instead of
    #    stalling the run.
    if enabled is None:
        enabled = is_ledger_enabled()
    if not enabled:
        return
    try:
        pd_store.record_cache_event(
            stage_tag, outcome,
            saved_seconds=saved_seconds,
            tool_seconds=tool_seconds,
            restore_seconds=restore_seconds,
            saved_cpu_seconds=saved_cpu_seconds,
            tool_cpu_seconds=tool_cpu_seconds,
            triggering_user=os.environ.get("HAMMER_AIRFLOW_TRIGGERING_USER") or None,
            dag_id=dag_id or None,
            dag_run_id=run_id or None,
            workspace=os.environ.get("HAMMER_AIRFLOW_WORKSPACE") or None,
            design=design,
            project=project,
        )
    except Exception:
        pass


def read_run_cache_summary(run_id: str) -> str:
    """
    Read all cache events for ``run_id`` and return a formatted multi-line
    summary suitable for printing in the ``exit_`` task.

    Returns an empty string (no header, no table) if no events file exists.
    """
    f = _events_file_for_run(run_id)
    if f is None or not f.exists():
        return ""
    events = []
    try:
        with f.open("r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return ""
    if not events:
        return ""

    # Track savings by source so we can report cache vs dep-check separately.
    # HIT              -> cache saved us (the dep-check said "run", cache said "no need")
    # SKIP_RESTORED    -> cache saved us (dep-check said skip, but local files
    #                     were missing - without the cache this would have
    #                     forced a re-run, so credit goes to cache)
    # SKIP_LOCAL       -> dep-check saved us (files were already on disk,
    #                     cache was never even consulted)
    # MISS_STORE       -> no savings; the tool actually ran
    # SKIP_NO_BLOB     -> no savings; the run probably failed
    cache_saved = 0.0       # HIT + SKIP_RESTORED  (wall-clock)
    depcheck_saved = 0.0    # SKIP_LOCAL           (wall-clock)
    total_ran = 0.0         # MISS_STORE           (wall-clock)
    cache_saved_cpu = 0.0   # HIT + SKIP_RESTORED  (CPU)
    depcheck_saved_cpu = 0.0  # SKIP_LOCAL         (CPU)
    total_ran_cpu = 0.0     # MISS_STORE           (CPU)

    def _wc(s: Optional[float]) -> str:
        """Format 'wall / cpu' pair for the detail column."""
        return _format_duration(s) if s is not None else "-"

    lines = []
    # Echo which design/project this run recorded under, so a UI-triggered run
    # can be checked from this task's log (set via conf {"project": ...} or config).
    design = next((e.get("design") for e in events if e.get("design")), None)
    project = next((e.get("project") for e in events if e.get("project")), None)
    lines.append(f"  Design:  {design or '(unset)'}")
    lines.append(f"  Project: {project or '(no project set)'}")
    lines.append("")
    header = (
        f"{'stage':<12}  {'outcome':<14}  {'attributed to':<14}  "
        f"{'wall':<10}  {'cpu':<10}  detail"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for ev in events:
        stage = ev.get("stage_tag", "?")
        outcome = ev.get("outcome", "?")
        saved = ev.get("saved_seconds")
        tool = ev.get("tool_seconds")
        restore = ev.get("restore_seconds")
        saved_cpu = ev.get("saved_cpu_seconds")
        tool_cpu = ev.get("tool_cpu_seconds")
        attribution = ""
        wall_col = "-"
        cpu_col = "-"
        detail = ""
        if outcome == "HIT" and saved is not None:
            attribution = "cache"
            wall_col = _wc(saved)
            cpu_col = _wc(saved_cpu)
            detail = f"restore {_format_duration(restore or 0)}"
            cache_saved += saved
            if saved_cpu is not None:
                cache_saved_cpu += saved_cpu
        elif outcome == "SKIP_LOCAL" and saved is not None:
            attribution = "dep-check"
            wall_col = _wc(saved)
            cpu_col = _wc(saved_cpu)
            detail = "no restore needed, files on disk"
            depcheck_saved += saved
            if saved_cpu is not None:
                depcheck_saved_cpu += saved_cpu
        elif outcome == "SKIP_RESTORED" and saved is not None:
            attribution = "cache"
            wall_col = _wc(saved)
            cpu_col = _wc(saved_cpu)
            detail = "dep-check skip, restored from cache"
            cache_saved += saved
            if saved_cpu is not None:
                cache_saved_cpu += saved_cpu
        elif outcome == "MISS_STORE" and tool is not None:
            attribution = "—"
            wall_col = _wc(tool)
            cpu_col = _wc(tool_cpu)
            detail = "ran, stored to cache"
            total_ran += tool
            if tool_cpu is not None:
                total_ran_cpu += tool_cpu
        elif outcome == "SKIP_NO_BLOB":
            attribution = "—"
            detail = "WARNING: dep-check skip but no cache blob — downstream may fail"
        elif saved is None and outcome in ("HIT", "SKIP_LOCAL", "SKIP_RESTORED"):
            # Cache blob predates the duration-tracking feature; we know
            # we skipped a tool run but can't quantify how much we saved.
            attribution = "cache" if outcome != "SKIP_LOCAL" else "dep-check"
            detail = "skipped tool run (original duration not recorded)"
        lines.append(
            f"{stage:<12}  {outcome:<14}  {attribution:<14}  "
            f"{wall_col:<10}  {cpu_col:<10}  {detail}"
        )
    lines.append("-" * len(header))
    total_saved = cache_saved + depcheck_saved
    total_saved_cpu = cache_saved_cpu + depcheck_saved_cpu

    def _pair(wall: float, cpu: float) -> str:
        return (
            f"wall {_format_duration(wall):<10}  "
            f"cpu {_format_duration(cpu):<10}"
        )

    lines.append(f"  Saved by cache:            {_pair(cache_saved, cache_saved_cpu)}")
    lines.append(f"  Saved by dependency check: {_pair(depcheck_saved, depcheck_saved_cpu)}")
    lines.append(f"  Total time saved:          {_pair(total_saved, total_saved_cpu)}")
    lines.append(f"  Stages that actually ran:  {_pair(total_ran, total_ran_cpu)}")
    return "\n".join(lines)


def clear_run_cache_events(run_id: str) -> None:
    """Delete the events file for a run (call after summary is printed)."""
    f = _events_file_for_run(run_id)
    if f is not None and f.exists():
        try:
            f.unlink()
        except Exception:
            pass


# ----- Cross-run savings aggregation (the time-saved tracker) -----
#
# read_run_cache_summary (above) tallies ONE run from its JSONL file. The
# helpers below tally MANY runs -- from the durable Postgres ledger
# (pd_store.fetch_cache_events) and/or the on-disk JSONL files -- so we can
# answer "how much wall-clock / CPU time did the PD cache save across this
# whole tapeout?". The per-event attribution here is the same as
# read_run_cache_summary's; keep the two in sync if the outcome rules change.


def _attribute_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Classify one cache event into the savings buckets.

    Returns ``{attribution, wall_saved, cpu_saved, wall_ran, cpu_ran,
    quantified}``. ``attribution`` is one of ``cache`` / ``dep-check`` /
    ``ran`` / ``none``. ``quantified`` is False for a skip we know happened but
    whose original duration wasn't recorded (blob predates duration tracking),
    so callers can count it as a hit without inflating the saved total.
    """
    outcome = ev.get("outcome")
    saved = ev.get("saved_seconds")
    tool = ev.get("tool_seconds")
    saved_cpu = ev.get("saved_cpu_seconds")
    tool_cpu = ev.get("tool_cpu_seconds")
    res = {"attribution": "none", "wall_saved": 0.0, "cpu_saved": 0.0,
           "wall_ran": 0.0, "cpu_ran": 0.0, "quantified": False}
    if outcome in ("HIT", "SKIP_RESTORED") and saved is not None:
        res.update(attribution="cache", wall_saved=float(saved),
                   cpu_saved=float(saved_cpu or 0.0), quantified=True)
    elif outcome == "SKIP_LOCAL" and saved is not None:
        res.update(attribution="dep-check", wall_saved=float(saved),
                   cpu_saved=float(saved_cpu or 0.0), quantified=True)
    elif outcome == "MISS_STORE" and tool is not None:
        res.update(attribution="ran", wall_ran=float(tool),
                   cpu_ran=float(tool_cpu or 0.0), quantified=True)
    elif outcome in ("HIT", "SKIP_RESTORED", "SKIP_LOCAL") and saved is None:
        # We skipped a tool run but the blob predates duration tracking.
        res.update(attribution=("dep-check" if outcome == "SKIP_LOCAL" else "cache"),
                   quantified=False)
    return res


def iter_jsonl_cache_events(events_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read every event from every ``*.jsonl`` under the cache_events dir.

    Defaults to ``$AIRFLOW_HOME/cache_events``. These files are per-run and the
    exit_ task deletes them after summarizing, so this only sees runs that
    haven't been cleaned up yet -- the durable Postgres ledger
    (pd_store.fetch_cache_events) is the complete cross-run source.
    """
    if events_dir is not None:
        d: Optional[Path] = Path(events_dir)
    else:
        d = _events_dir()
    if d is None or not d.is_dir():
        return []
    events: List[Dict[str, Any]] = []
    for jf in sorted(d.glob("*.jsonl")):
        try:
            with jf.open("r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            continue
    return events


def _group_key(ev: Dict[str, Any], group_by: str) -> str:
    if group_by in ("stage", "stage_tag"):
        return ev.get("stage_tag") or "(unknown)"
    if group_by in ("dag", "dag_id"):
        return ev.get("dag_id") or "(none)"
    if group_by == "design":
        return ev.get("design") or ev.get("dag_id") or "(unknown)"
    if group_by == "project":
        return ev.get("project") or "(unassigned)"
    if group_by in ("run", "run_id"):
        return ev.get("run_id") or ev.get("dag_run_id") or "(none)"
    return "all"


def _empty_bucket() -> Dict[str, Any]:
    return {"events": 0, "hits": 0, "misses": 0, "skips": 0, "warnings": 0,
            "unquantified": 0, "cache_wall": 0.0, "cache_cpu": 0.0,
            "depcheck_wall": 0.0, "depcheck_cpu": 0.0,
            "ran_wall": 0.0, "ran_cpu": 0.0}


def _accumulate(bucket: Dict[str, Any], ev: Dict[str, Any]) -> None:
    bucket["events"] += 1
    outcome = ev.get("outcome")
    if outcome == "MISS_STORE":
        bucket["misses"] += 1
    elif outcome == "HIT":
        bucket["hits"] += 1
    elif outcome in ("SKIP_LOCAL", "SKIP_RESTORED"):
        bucket["skips"] += 1
    elif outcome == "SKIP_NO_BLOB":
        bucket["warnings"] += 1
    a = _attribute_event(ev)
    if a["attribution"] == "cache":
        bucket["cache_wall"] += a["wall_saved"]
        bucket["cache_cpu"] += a["cpu_saved"]
    elif a["attribution"] == "dep-check":
        bucket["depcheck_wall"] += a["wall_saved"]
        bucket["depcheck_cpu"] += a["cpu_saved"]
    elif a["attribution"] == "ran":
        bucket["ran_wall"] += a["wall_ran"]
        bucket["ran_cpu"] += a["cpu_ran"]
    if a["attribution"] in ("cache", "dep-check") and not a["quantified"]:
        bucket["unquantified"] += 1


def aggregate_savings(events: List[Dict[str, Any]],
                      group_by: str = "stage") -> Dict[str, Any]:
    """Total cache savings over a list of events, with a per-group breakdown."""
    totals = _empty_bucket()
    groups: Dict[str, Dict[str, Any]] = {}
    runs = set()
    dags = set()
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    for ev in events:
        _accumulate(totals, ev)
        gk = _group_key(ev, group_by)
        _accumulate(groups.setdefault(gk, _empty_bucket()), ev)
        rid = ev.get("run_id") or ev.get("dag_run_id")
        if rid:
            runs.add(rid)
        if ev.get("dag_id"):
            dags.add(ev.get("dag_id"))
        ts = ev.get("ts")
        if isinstance(ts, (int, float)):
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
    return {"totals": totals, "groups": groups, "group_by": group_by,
            "n_runs": len(runs), "n_dags": len(dags),
            "first_ts": first_ts, "last_ts": last_ts}


def format_savings_report(events: List[Dict[str, Any]],
                          group_by: str = "stage",
                          source: str = "") -> str:
    """Render a human-readable cross-run time-saved report."""
    agg = aggregate_savings(events, group_by=group_by)
    t = agg["totals"]
    if t["events"] == 0:
        return "(no cache events found)"

    total_saved_wall = t["cache_wall"] + t["depcheck_wall"]
    total_saved_cpu = t["cache_cpu"] + t["depcheck_cpu"]
    decided = t["hits"] + t["misses"]
    hit_rate = (100.0 * t["hits"] / decided) if decided else 0.0

    label = {"stage": "stage", "stage_tag": "stage", "dag": "dag",
             "dag_id": "dag", "design": "design", "project": "project",
             "run": "run", "run_id": "run", "none": "all"}.get(group_by, group_by)

    lines: List[str] = []
    src = f"  (source: {source})" if source else ""
    lines.append(f"PD cache time saved{src}")
    if agg["first_ts"] and agg["last_ts"]:
        span = (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(agg['first_ts']))}"
                f"  ->  {time.strftime('%Y-%m-%d %H:%M', time.localtime(agg['last_ts']))}")
        lines.append(f"  span: {span}   runs: {agg['n_runs']}   dags: {agg['n_dags']}")
    header = (f"{label:<34}  {'events':>6}  {'hits':>5}  {'miss':>5}  "
              f"{'wall saved':>12}  {'cpu saved':>12}  {'wall ran':>12}")
    lines.append("")
    lines.append(header)
    lines.append("-" * len(header))
    for gk in sorted(agg["groups"], key=lambda k: -(agg["groups"][k]["cache_wall"]
                                                     + agg["groups"][k]["depcheck_wall"])):
        b = agg["groups"][gk]
        saved_w = b["cache_wall"] + b["depcheck_wall"]
        lines.append(
            f"{_short(gk, 34):<34}  {b['events']:>6}  {b['hits']:>5}  {b['misses']:>5}  "
            f"{_format_duration(saved_w):>12}  {_format_duration(b['cache_cpu'] + b['depcheck_cpu']):>12}  "
            f"{_format_duration(b['ran_wall']):>12}")
    lines.append("-" * len(header))

    def _pair(wall: float, cpu: float) -> str:
        return f"wall {_format_duration(wall):<12}  cpu {_format_duration(cpu):<12}"

    lines.append(f"  Saved by cache:            {_pair(t['cache_wall'], t['cache_cpu'])}")
    lines.append(f"  Saved by dependency check: {_pair(t['depcheck_wall'], t['depcheck_cpu'])}")
    lines.append(f"  TOTAL TIME SAVED:          {_pair(total_saved_wall, total_saved_cpu)}")
    lines.append(f"  Time that actually ran:    {_pair(t['ran_wall'], t['ran_cpu'])}")
    lines.append(f"  Cache hits / misses:       {t['hits']} hit, {t['misses']} miss "
                 f"({hit_rate:.0f}% hit rate over {decided} decided stages)")
    if t["unquantified"]:
        lines.append(f"  Note: {t['unquantified']} hit(s) had no recorded duration "
                     f"(blob predates duration tracking) and are excluded from the saved total.")
    return "\n".join(lines)


def _short(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."


def _jsonl_matches(ev: Dict[str, Any], *, since: Optional[float], until: Optional[float],
                   dag: Optional[str], design: Optional[str], stage: Optional[str],
                   user: Optional[str], project: Optional[str] = None) -> bool:
    """Apply the same filters fetch_cache_events applies in SQL, to a JSONL event."""
    ts = ev.get("ts")
    if since is not None and isinstance(ts, (int, float)) and ts < since:
        return False
    if until is not None and isinstance(ts, (int, float)) and ts > until:
        return False
    if dag and dag.lower() not in str(ev.get("dag_id") or "").lower():
        return False
    if design and design.lower() not in str(ev.get("design") or "").lower():
        return False
    if stage and stage.lower() not in str(ev.get("stage_tag") or "").lower():
        return False
    if project and project.lower() not in str(ev.get("project") or "").lower():
        return False
    if user:
        u = user.lower()
        if u not in str(ev.get("triggering_user") or "").lower() \
           and u not in str(ev.get("owner") or "").lower():
            return False
    return True


def collect_savings_events(
    *,
    source: str = "auto",
    since: Optional[float] = None,
    until: Optional[float] = None,
    dag: Optional[str] = None,
    design: Optional[str] = None,
    stage: Optional[str] = None,
    user: Optional[str] = None,
    project: Optional[str] = None,
    limit: Optional[int] = None,
    events_dir: Optional[str] = None,
):
    """Gather cache events for the time-saved report. Returns (events, label).

    ``source`` is one of:
      * ``auto`` (default) -- read the durable Postgres ledger; if it's
        unreachable or empty, fall back to the on-disk JSONL files.
      * ``db``   -- Postgres ledger only (raises if unreachable).
      * ``jsonl``-- on-disk JSONL files only (no DB needed).
      * ``both`` -- concatenate DB + JSONL (may double-count a run that is in
        the ledger AND still has an un-cleared JSONL file).
    """
    def _db():
        return pd_store.fetch_cache_events(
            since=since, until=until, dag=dag, design=design,
            stage=stage, user=user, project=project, limit=limit)

    def _jsonl():
        evs = iter_jsonl_cache_events(events_dir)
        return [e for e in evs if _jsonl_matches(
            e, since=since, until=until, dag=dag, design=design,
            stage=stage, user=user, project=project)]

    if source == "jsonl":
        return _jsonl(), "jsonl files"
    if source == "db":
        return _db(), "postgres ledger"
    if source == "both":
        try:
            db = _db()
        except Exception:
            db = []
        return db + _jsonl(), "postgres ledger + jsonl files"
    # auto
    try:
        db = _db()
    except Exception as e:
        return _jsonl(), f"jsonl files (DB unavailable: {e})"
    if db:
        return db, "postgres ledger"
    return _jsonl(), "jsonl files (ledger empty)"


CACHE_ENV_VAR = "HAMMER_PD_CACHE"
CACHE_SETTING_KEY = "vlsi.pd_cache.enabled"

# Recording switch for the durable time-saved ledger (the pd_cache_events
# Postgres table). Separate from the cache itself: the cache can be on while
# ledger recording is off (you still get HITs, just no durable savings rows).
LEDGER_ENV_VAR = "HAMMER_PD_CACHE_LEDGER"
LEDGER_SETTING_KEY = "vlsi.pd_cache.ledger_enabled"

# Optional human-assigned project label stamped onto each ledger row, for
# bucketing several designs/dags under one tapeout. Env var wins; the config
# key is a fallback so a design can pin its project in yml.
PROJECT_ENV_VAR = "HAMMER_PD_PROJECT"
PROJECT_SETTING_KEY = "vlsi.pd_cache.project"


def is_cache_enabled(driver: Optional[Any] = None) -> bool:
    """True if the env var or the driver's setting opts the cache on."""
    env = os.environ.get(CACHE_ENV_VAR, "")
    if env not in ("", "0", "false", "False", "no"):
        return True
    if driver is not None:
        try:
            val = driver.database.get_setting(CACHE_SETTING_KEY, nullvalue=False)
            if isinstance(val, bool) and val:
                return True
            if isinstance(val, str) and val.lower() in ("1", "true", "yes"):
                return True
        except Exception:
            pass
    return False


def is_ledger_enabled(driver: Optional[Any] = None) -> bool:
    """True if the durable time-saved ledger should record cache events.

    This is the tracker's recording switch. It gates ONLY the durable Postgres
    rows (pd_store.record_cache_event); the per-run JSONL summary that feeds the
    exit_ task is unaffected. Defaults to on.

    Turn it off with ``HAMMER_PD_CACHE_LEDGER=0`` (or ``=false/no/off``), or in
    config with ``vlsi.pd_cache.ledger_enabled: false``. The env var wins; the
    config key is consulted only when the env var is unset and a driver is
    given. Set the env var in the Airflow worker env (e.g. venv.sh) to control
    DAG runs centrally.
    """
    env = os.environ.get(LEDGER_ENV_VAR)
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    if driver is not None:
        try:
            val = driver.database.get_setting(LEDGER_SETTING_KEY, nullvalue=None)
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            pass
    return True


def _stamp_project_from_config(driver: Optional[Any]) -> None:
    """If no project env var is set, pull one from config (vlsi.pd_cache.project).

    Stamps HAMMER_PD_PROJECT so _record_cache_event (which reads the env) tags
    rows with it. A shell/worker env var or the DAG trigger conf both win over
    the config key, since either would already have set the env var.
    """
    if os.environ.get(PROJECT_ENV_VAR) or driver is None:
        return
    try:
        val = driver.database.get_setting(PROJECT_SETTING_KEY, nullvalue=None)
        if val:
            os.environ[PROJECT_ENV_VAR] = str(val)
    except Exception:
        pass


def _build_cache_key(driver: Any, stage_tag: str) -> str:
    """Compute the stage cache key from the live driver config."""
    db_json = driver.database.get_database_json()
    db: Dict[str, Any] = json.loads(db_json)

    rtl_files = db.get("synthesis.inputs.input_files") or []
    rtl_files = [f for f in rtl_files if isinstance(f, str)]
    if rtl_files:
        try:
            db["vlsi.rtl_fingerprint_sha256"] = pd_store.compute_rtl_fingerprint(rtl_files)
        except Exception:
            pass

    return pd_store.compute_stage_key(db, stage_tag)


def cache_or_run(
    driver: Any,
    stage_tag: str,
    rundir: str,
    output_filename: str,
    run_fn: Callable[[], Tuple[bool, Dict[str, Any]]],
    force_local: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Cache wrapper around a stage's run function.

    Args:
        driver: HammerDriver instance, used for config and logging.
        stage_tag: One of pd_store.KNOWN_STAGE_TAGS.
        rundir: Absolute path to the stage's run directory (e.g. driver.syn_tool.run_dir).
        output_filename: Name of the per-stage output JSON inside rundir
            (e.g. "syn-output.json"). Used to reconstruct the output dict on
            cache hit.
        run_fn: Callable that actually runs the stage and returns (success, output).
        force_local: If True (the --local flag), skip restoring from the Postgres
            cache and run the stage locally. The fresh result is still STORED, so
            a local re-run refreshes the shared cache rather than ignoring it.
            Dependency checks are unaffected -- that's the separate --force flag.

    Returns:
        (success, output) tuple, identical in shape to run_fn's return value.
    """
    log = getattr(driver, "log", None)

    def _info(msg: str) -> None:
        if log is not None:
            log.info(msg)

    def _warn(msg: str) -> None:
        if log is not None:
            log.warning(msg)

    if not is_cache_enabled(driver):
        return run_fn()

    # Resolve the ledger switch once (honors env var + config key) and pass it
    # to each event record so the time-saved tracker can be turned off.
    ledger_on = is_ledger_enabled(driver)
    _stamp_project_from_config(driver)

    try:
        key = _build_cache_key(driver, stage_tag)
    except Exception as e:
        _warn(f"PD cache: key computation failed ({e}); running {stage_tag} normally.")
        return run_fn()

    short = key[:16]

    if force_local:
        # --local: skip the DB restore entirely and run the tool locally. We
        # still computed the key above and STILL store the fresh result below,
        # so a local re-run refreshes the shared cache instead of bypassing it.
        _info(f"PD cache: --local set; skipping DB lookup for {stage_tag}, running locally.")
        blob = None
    else:
        try:
            blob = pd_store.load_stage_blob(key)
        except Exception as e:
            _warn(f"PD cache: lookup failed ({e}); running {stage_tag} normally.")
            return run_fn()

    if blob is not None:
        _, data, original_duration, original_cpu = blob
        rundir_path = Path(rundir)
        try:
            t0 = time.monotonic()
            rundir_path.parent.mkdir(parents=True, exist_ok=True)
            pd_store.untar_to_directory(data, rundir_path.parent)
            output_path = rundir_path / output_filename
            with output_path.open("r") as f:
                output = json.load(f)
            restore_seconds = time.monotonic() - t0
            saved = None
            saved_cpu = None
            if original_duration is not None:
                saved = max(0.0, original_duration - restore_seconds)
            if original_cpu is not None:
                # Restore is a single-threaded tar untar, so its CPU cost ~=
                # its wall-clock cost. Use that to net it out of the saved-CPU.
                saved_cpu = max(0.0, original_cpu - restore_seconds)
            _record_cache_event(
                stage_tag, "HIT",
                saved_seconds=saved,
                restore_seconds=restore_seconds,
                saved_cpu_seconds=saved_cpu,
                enabled=ledger_on,
            )
            _info(
                f"PD cache HIT for {stage_tag} (sha256={short}...). "
                f"Restored {rundir_path}, skipping run."
                f"{_format_savings(original_duration, restore_seconds, original_cpu)}"
            )
            return True, output
        except Exception as e:
            _warn(
                f"PD cache: hit but restore failed ({e}); running {stage_tag} normally."
            )

    _info(f"PD cache MISS for {stage_tag} (sha256={short}...). Running stage.")
    t0 = time.monotonic()
    cpu0 = _child_cpu_seconds()
    success, output = run_fn()
    duration_seconds = time.monotonic() - t0
    cpu1 = _child_cpu_seconds()
    cpu_seconds: Optional[float] = None
    if cpu0 is not None and cpu1 is not None:
        cpu_seconds = max(0.0, cpu1 - cpu0)
    if success:
        try:
            # Write the stage's output dict into the rundir BEFORE we tar it.
            # cli_driver writes <stage>-output.json after we return, but we
            # need it in the tarball so HIT-path restores can read it back.
            output_path = Path(rundir) / output_filename
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                from hammer.config import HammerJSONEncoder
                with output_path.open("w") as f:
                    json.dump(output, f, cls=HammerJSONEncoder, indent=4)
            except Exception as e:
                _warn(f"PD cache: could not pre-write {output_filename} ({e}); tarball may lack it.")

            tarball = pd_store.tar_directory(Path(rundir))
            pd_store.store_stage_blob(
                stage_tag, key, tarball,
                duration_seconds=duration_seconds,
                cpu_seconds=cpu_seconds,
                triggering_user=os.environ.get("HAMMER_AIRFLOW_TRIGGERING_USER") or None,
                dag_id=os.environ.get("HAMMER_AIRFLOW_DAG_ID") or None,
                dag_run_id=os.environ.get("HAMMER_AIRFLOW_RUN_ID") or None,
                workspace=os.environ.get("HAMMER_AIRFLOW_WORKSPACE") or None,
                design=os.environ.get("HAMMER_AIRFLOW_DESIGN") or os.environ.get("design") or None,
            )
            _record_cache_event(
                stage_tag, "MISS_STORE",
                tool_seconds=duration_seconds,
                tool_cpu_seconds=cpu_seconds,
                enabled=ledger_on,
            )
            cpu_msg = (
                f", CPU={_format_duration(cpu_seconds)}"
                if cpu_seconds is not None else ""
            )
            _info(
                f"PD cache STORE {stage_tag} (sha256={short}..., "
                f"bytes={len(tarball)}, "
                f"tool runtime={_format_duration(duration_seconds)}{cpu_msg})."
            )
        except Exception as e:
            _warn(f"PD cache: store failed ({e}); continuing.")
    return success, output


def try_restore_from_cache(
    driver: Any,
    stage_tag: str,
    rundir: str,
    output_filename: str,
) -> bool:
    """
    Lighter sibling of cache_or_run, used in the "stage_change_check says skip"
    branch of cli_driver.py.

    Situation: Hammer's dependency tracker has decided the stage doesn't need
    to rerun (config + inputs unchanged since the last commit_master_database).
    But our local rundir might have been wiped since then (e.g. disk cleanup,
    fresh checkout, or any "I cleared the build dir but kept master_database"
    flow). If the rundir's output_filename is missing on disk, we'd skip the
    stage AND have nothing for downstream stages to read. Bad.

    This function attempts to restore from cache instead. Returns True if it
    restored a tarball, False if it didn't (cache disabled, no matching blob,
    or restore failed). On True the caller can proceed as if the stage ran.
    """
    log = getattr(driver, "log", None)

    def _info(msg: str) -> None:
        if log is not None:
            log.info(msg)

    def _warn(msg: str) -> None:
        if log is not None:
            log.warning(msg)

    if not is_cache_enabled(driver):
        return False

    ledger_on = is_ledger_enabled(driver)
    _stamp_project_from_config(driver)

    output_path = Path(rundir) / output_filename
    if output_path.exists():
        # Local files still present, nothing to do. The skip is safe.
        # Still helpful to surface the savings vs a fresh tool run.
        original_duration: Optional[float] = None
        original_cpu: Optional[float] = None
        try:
            key = _build_cache_key(driver, stage_tag)
            blob_meta = pd_store.load_stage_blob(key)
            if blob_meta is not None:
                _, _, original_duration, original_cpu = blob_meta
                if original_duration:
                    cpu_msg = (
                        f"; {_format_duration(original_cpu)} CPU"
                        if original_cpu else ""
                    )
                    _info(
                        f"PD cache (skip-path) for {stage_tag}: stage_change_check "
                        f"says skip, local rundir present. "
                        f"Saved ~{_format_duration(original_duration)} wall"
                        f"{cpu_msg} (original tool runtime) by not re-running."
                    )
        except Exception:
            pass
        _record_cache_event(
            stage_tag, "SKIP_LOCAL",
            saved_seconds=original_duration,
            saved_cpu_seconds=original_cpu,
            enabled=ledger_on,
        )
        return True

    try:
        key = _build_cache_key(driver, stage_tag)
    except Exception as e:
        _warn(f"PD cache (skip-path): key computation failed ({e}); not restoring.")
        return False

    short = key[:16]

    try:
        blob = pd_store.load_stage_blob(key)
    except Exception as e:
        _warn(f"PD cache (skip-path): lookup failed ({e}); not restoring.")
        return False

    if blob is None:
        _record_cache_event(stage_tag, "SKIP_NO_BLOB", enabled=ledger_on)
        _warn(
            f"PD cache (skip-path): stage_change_check would skip {stage_tag}, "
            f"but no local {output_filename} and no matching cache blob "
            f"(sha256={short}...). Downstream stages will likely fail."
        )
        return False

    _, data, original_duration, original_cpu = blob
    rundir_path = Path(rundir)
    try:
        t0 = time.monotonic()
        rundir_path.parent.mkdir(parents=True, exist_ok=True)
        pd_store.untar_to_directory(data, rundir_path.parent)
        restore_seconds = time.monotonic() - t0
        saved = None
        saved_cpu = None
        if original_duration is not None:
            saved = max(0.0, original_duration - restore_seconds)
        if original_cpu is not None:
            saved_cpu = max(0.0, original_cpu - restore_seconds)
        _record_cache_event(
            stage_tag, "SKIP_RESTORED",
            saved_seconds=saved,
            restore_seconds=restore_seconds,
            saved_cpu_seconds=saved_cpu,
            enabled=ledger_on,
        )
        _info(
            f"PD cache HIT (skip-path) for {stage_tag} (sha256={short}...). "
            f"stage_change_check said skip, local rundir was missing; "
            f"restored from cache."
            f"{_format_savings(original_duration, restore_seconds, original_cpu)}"
        )
        return True
    except Exception as e:
        _warn(f"PD cache (skip-path): hit but restore failed ({e}).")
        return False

