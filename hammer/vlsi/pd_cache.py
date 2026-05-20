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
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from hammer.vlsi import pd_store


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
        _, data = blob
        rundir_path = Path(rundir)
        try:
            rundir_path.parent.mkdir(parents=True, exist_ok=True)
            pd_store.untar_to_directory(data, rundir_path.parent)
            output_path = rundir_path / output_filename
            with output_path.open("r") as f:
                output = json.load(f)
            _info(
                f"PD cache HIT for {stage_tag} (sha256={short}...). "
                f"Restored {rundir_path}, skipping run."
            )
            return True, output
        except Exception as e:
            _warn(
                f"PD cache: hit but restore failed ({e}); running {stage_tag} normally."
            )

    _info(f"PD cache MISS for {stage_tag} (sha256={short}...). Running stage.")
    success, output = run_fn()
    if success:
        try:
            tarball = pd_store.tar_directory(Path(rundir))
            pd_store.store_stage_blob(stage_tag, key, tarball)
            _info(
                f"PD cache STORE {stage_tag} (sha256={short}..., bytes={len(tarball)})."
            )
        except Exception as e:
            _warn(f"PD cache: store failed ({e}); continuing.")
    return success, output
