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

# The time-saved tracker (event recording, per-run summary, cross-run
# reporting, ledger/project switches) lives in time_tracking.py. The cache
# imports what it needs inline and re-exports the tracker's public names so
# existing callers -- including already-generated DAGs that do
# `from hammer.vlsi.pd_cache import read_run_cache_summary` -- keep working.
from hammer.vlsi.time_tracking import (
    LEDGER_ENV_VAR, LEDGER_SETTING_KEY,
    PROJECT_ENV_VAR, PROJECT_SETTING_KEY,
    is_ledger_enabled,
    record_event as _record_cache_event,
    stamp_project_from_config as _stamp_project_from_config,
    make_would_rerun as _make_would_rerun,
    stage_module as _stage_module,
    _format_duration, _format_savings,
    read_run_cache_summary, clear_run_cache_events,
    iter_jsonl_cache_events, aggregate_savings, format_savings_report,
    collect_savings_events, exclude_depcheck_skips,
)


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
    module = _stage_module(driver, stage_tag)

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
            # time the restore from BEFORE the fetch: shipping the blob from
            # Postgres over the network is the dominant overhead legacy never
            # pays, so it must be inside restore_seconds (and thus netted out
            # of the saved time below).
            _restore_t0 = time.monotonic()
            blob = pd_store.load_stage_blob(key)
        except Exception as e:
            _warn(f"PD cache: lookup failed ({e}); running {stage_tag} normally.")
            return run_fn()

    if blob is not None:
        _, data, original_duration, original_cpu = blob
        rundir_path = Path(rundir)
        try:
            rundir_path.parent.mkdir(parents=True, exist_ok=True)
            pd_store.untar_to_directory(data, rundir_path.parent)
            output_path = rundir_path / output_filename
            with output_path.open("r") as f:
                output = json.load(f)
            restore_seconds = time.monotonic() - _restore_t0
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
                module=module,
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

            # store cost = tarring/compressing the rundir + uploading the blob
            # to Postgres. This is the one-time overhead SledgeHammer pays on a
            # miss that legacy does not; recorded for visibility, NOT netted
            # into any savings total here.
            _store_t0 = time.monotonic()
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
            store_seconds = time.monotonic() - _store_t0
            _record_cache_event(
                stage_tag, "MISS_STORE",
                tool_seconds=duration_seconds,
                tool_cpu_seconds=cpu_seconds,
                store_seconds=store_seconds,
                module=module,
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
    module = _stage_module(driver, stage_tag)

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
            # Would legacy make's mtime rule have rerun this stage? True means
            # this skip is a SledgeHammer-only saving (content-aware dep-check
            # beat make); False means legacy would have skipped it too.
            make_would_rerun=_make_would_rerun(driver, output_path),
            module=module,
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
        # time from before the fetch so the Postgres/network transfer counts
        _restore_t0 = time.monotonic()
        blob = pd_store.load_stage_blob(key)
    except Exception as e:
        _warn(f"PD cache (skip-path): lookup failed ({e}); not restoring.")
        return False

    if blob is None:
        _record_cache_event(stage_tag, "SKIP_NO_BLOB", module=module, enabled=ledger_on)
        _warn(
            f"PD cache (skip-path): stage_change_check would skip {stage_tag}, "
            f"but no local {output_filename} and no matching cache blob "
            f"(sha256={short}...). Downstream stages will likely fail."
        )
        return False

    _, data, original_duration, original_cpu = blob
    rundir_path = Path(rundir)
    try:
        rundir_path.parent.mkdir(parents=True, exist_ok=True)
        pd_store.untar_to_directory(data, rundir_path.parent)
        restore_seconds = time.monotonic() - _restore_t0
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
            # The output file was missing, so legacy make would rebuild
            # unconditionally: this restore is a SledgeHammer-only saving.
            make_would_rerun=True,
            module=module,
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

