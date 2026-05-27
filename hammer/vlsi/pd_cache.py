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
from typing import Any, Callable, Dict, Optional, Tuple

from hammer.vlsi import pd_store


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


def _format_savings(original_duration: Optional[float], restore_seconds: float) -> str:
    """Build the '(saved ~Xs vs original Ys tool run)' suffix, or '' if unknown."""
    if original_duration is None or original_duration <= 0:
        return ""
    saved = max(0.0, original_duration - restore_seconds)
    return (
        f" Saved ~{_format_duration(saved)} "
        f"(restore {_format_duration(restore_seconds)} "
        f"vs original {_format_duration(original_duration)} tool run)."
    )


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
) -> None:
    """
    Append a single cache event for the current DAG run.

    Outcomes: ``HIT``, ``MISS_STORE``, ``SKIP_LOCAL`` (dep-check skip,
    local files were already on disk), ``SKIP_RESTORED`` (dep-check skip,
    files missing so restored from tarball), ``SKIP_NO_BLOB`` (dep-check
    skip, no local files AND no cache blob - downstream will likely fail).

    Silently no-ops outside an Airflow run (no AIRFLOW_HOME or no run_id).
    """
    run_id = os.environ.get("HAMMER_AIRFLOW_RUN_ID", "")
    dag_id = os.environ.get("HAMMER_AIRFLOW_DAG_ID", "")
    f = _events_file_for_run(run_id)
    if f is None:
        return
    event = {
        "ts":              time.time(),
        "dag_id":          dag_id,
        "run_id":          run_id,
        "stage_tag":       stage_tag,
        "outcome":         outcome,
        "saved_seconds":   saved_seconds,
        "tool_seconds":    tool_seconds,
        "restore_seconds": restore_seconds,
    }
    try:
        with f.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception:
        # Telemetry must never fail the run.
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
    cache_saved = 0.0       # HIT + SKIP_RESTORED
    depcheck_saved = 0.0    # SKIP_LOCAL
    total_ran = 0.0         # MISS_STORE
    lines = []
    header = f"{'stage':<12}  {'outcome':<14}  {'attributed to':<14}  {'detail':<48}"
    lines.append(header)
    lines.append("-" * len(header))
    for ev in events:
        stage = ev.get("stage_tag", "?")
        outcome = ev.get("outcome", "?")
        saved = ev.get("saved_seconds")
        tool = ev.get("tool_seconds")
        restore = ev.get("restore_seconds")
        attribution = ""
        detail = ""
        if outcome == "HIT" and saved is not None:
            attribution = "cache"
            detail = f"saved {_format_duration(saved)} (restore {_format_duration(restore or 0)})"
            cache_saved += saved
        elif outcome == "SKIP_LOCAL" and saved is not None:
            attribution = "dep-check"
            detail = f"saved {_format_duration(saved)} (no restore needed, files on disk)"
            depcheck_saved += saved
        elif outcome == "SKIP_RESTORED" and saved is not None:
            attribution = "cache"
            detail = f"saved {_format_duration(saved)} (dep-check skip, restored from cache)"
            cache_saved += saved
        elif outcome == "MISS_STORE" and tool is not None:
            attribution = "—"
            detail = f"ran {_format_duration(tool)} (stored to cache)"
            total_ran += tool
        elif outcome == "SKIP_NO_BLOB":
            attribution = "—"
            detail = "WARNING: dep-check skip but no cache blob — downstream may fail"
        elif saved is None and outcome in ("HIT", "SKIP_LOCAL", "SKIP_RESTORED"):
            # Cache blob predates the duration-tracking feature; we know
            # we skipped a tool run but can't quantify how much we saved.
            attribution = "cache" if outcome != "SKIP_LOCAL" else "dep-check"
            detail = "skipped tool run (original duration not recorded)"
        lines.append(f"{stage:<12}  {outcome:<14}  {attribution:<14}  {detail:<48}")
    lines.append("-" * len(header))
    total_saved = cache_saved + depcheck_saved
    lines.append(f"  Saved by cache:           {_format_duration(cache_saved)}")
    lines.append(f"  Saved by dependency check:{_format_duration(depcheck_saved):>10}")
    lines.append(f"  Total time saved:         {_format_duration(total_saved)}")
    lines.append(f"  Stages that actually ran: {_format_duration(total_ran)}")
    return "\n".join(lines)


def clear_run_cache_events(run_id: str) -> None:
    """Delete the events file for a run (call after summary is printed)."""
    f = _events_file_for_run(run_id)
    if f is not None and f.exists():
        try:
            f.unlink()
        except Exception:
            pass


CACHE_ENV_VAR = "HAMMER_PD_CACHE"
CACHE_SETTING_KEY = "vlsi.pd_cache.enabled"


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

    try:
        key = _build_cache_key(driver, stage_tag)
    except Exception as e:
        _warn(f"PD cache: key computation failed ({e}); running {stage_tag} normally.")
        return run_fn()

    short = key[:16]

    try:
        blob = pd_store.load_stage_blob(key)
    except Exception as e:
        _warn(f"PD cache: lookup failed ({e}); running {stage_tag} normally.")
        return run_fn()

    if blob is not None:
        _, data, original_duration = blob
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
            if original_duration is not None:
                saved = max(0.0, original_duration - restore_seconds)
            _record_cache_event(
                stage_tag, "HIT",
                saved_seconds=saved,
                restore_seconds=restore_seconds,
            )
            _info(
                f"PD cache HIT for {stage_tag} (sha256={short}...). "
                f"Restored {rundir_path}, skipping run."
                f"{_format_savings(original_duration, restore_seconds)}"
            )
            return True, output
        except Exception as e:
            _warn(
                f"PD cache: hit but restore failed ({e}); running {stage_tag} normally."
            )

    _info(f"PD cache MISS for {stage_tag} (sha256={short}...). Running stage.")
    t0 = time.monotonic()
    success, output = run_fn()
    duration_seconds = time.monotonic() - t0
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
                triggering_user=os.environ.get("HAMMER_AIRFLOW_TRIGGERING_USER") or None,
                dag_id=os.environ.get("HAMMER_AIRFLOW_DAG_ID") or None,
                dag_run_id=os.environ.get("HAMMER_AIRFLOW_RUN_ID") or None,
                workspace=os.environ.get("HAMMER_AIRFLOW_WORKSPACE") or None,
                design=os.environ.get("design") or None,
            )
            _record_cache_event(
                stage_tag, "MISS_STORE",
                tool_seconds=duration_seconds,
            )
            _info(
                f"PD cache STORE {stage_tag} (sha256={short}..., "
                f"bytes={len(tarball)}, tool runtime={_format_duration(duration_seconds)})."
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

    output_path = Path(rundir) / output_filename
    if output_path.exists():
        # Local files still present, nothing to do. The skip is safe.
        # Still helpful to surface the savings vs a fresh tool run.
        original_duration = None
        try:
            key = _build_cache_key(driver, stage_tag)
            blob_meta = pd_store.load_stage_blob(key)
            if blob_meta is not None:
                _, _, original_duration = blob_meta
                if original_duration:
                    _info(
                        f"PD cache (skip-path) for {stage_tag}: stage_change_check "
                        f"says skip, local rundir present. "
                        f"Saved ~{_format_duration(original_duration)} "
                        f"(original tool runtime) by not re-running."
                    )
        except Exception:
            pass
        _record_cache_event(
            stage_tag, "SKIP_LOCAL",
            saved_seconds=original_duration,
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
        _record_cache_event(stage_tag, "SKIP_NO_BLOB")
        _warn(
            f"PD cache (skip-path): stage_change_check would skip {stage_tag}, "
            f"but no local {output_filename} and no matching cache blob "
            f"(sha256={short}...). Downstream stages will likely fail."
        )
        return False

    _, data, original_duration = blob
    rundir_path = Path(rundir)
    try:
        t0 = time.monotonic()
        rundir_path.parent.mkdir(parents=True, exist_ok=True)
        pd_store.untar_to_directory(data, rundir_path.parent)
        restore_seconds = time.monotonic() - t0
        saved = None
        if original_duration is not None:
            saved = max(0.0, original_duration - restore_seconds)
        _record_cache_event(
            stage_tag, "SKIP_RESTORED",
            saved_seconds=saved,
            restore_seconds=restore_seconds,
        )
        _info(
            f"PD cache HIT (skip-path) for {stage_tag} (sha256={short}...). "
            f"stage_change_check said skip, local rundir was missing; "
            f"restored from cache."
            f"{_format_savings(original_duration, restore_seconds)}"
        )
        return True
    except Exception as e:
        _warn(f"PD cache (skip-path): hit but restore failed ({e}).")
        return False

