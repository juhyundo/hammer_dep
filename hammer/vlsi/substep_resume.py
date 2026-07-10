"""
Automatic sub-step resume for partial tool runs.

Genus and innovus write a checkpoint database at every step boundary
(``write_db pre_<step>``) as the generated script executes. Upstream hammer
can resume from one, but only when the user diagnoses which step failed and
passes ``--from_step`` by hand. This module automates that: when a stage is
about to run and the previous attempt died partway (or was stopped with
``--to_step``), pick the newest checkpoint the tool CONFIRMED writing, check
the inputs haven't changed since that attempt, and resume from there instead
of starting over.

Trust rules, in order:

  * A checkpoint counts only if the tool's log confirms the write finished
    (genus prints "Finished exporting design database to file 'pre_X'") and
    the file is still on disk. Newest confirmed wins, not newest mtime, so a
    write the tool died inside of is never trusted.
  * The marker file's stage key (the same config+RTL fingerprint the PD cache
    uses) must match the current inputs. A mismatch means the checkpoints
    describe a different design state: they are deleted and the run starts
    from scratch.
  * A resume that makes no progress burns its checkpoint: the next attempt
    uses the next older confirmed one, and when the ladder is exhausted the
    run starts from scratch. No resume loops on a corrupt database.

Explicit flow control (``--from_step`` and friends) and ``--force`` always
win over auto-resume. Gate with ``vlsi.substep_resume.enabled`` or
``HAMMER_SUBSTEP_RESUME=0`` (default on).
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MARKER_NAME = ".substep_resume.json"
ENABLE_ENV_VAR = "HAMMER_SUBSTEP_RESUME"
ENABLE_SETTING_KEY = "vlsi.substep_resume.enabled"

# Tool-confirmed checkpoint write, per tool log. Genus example:
#   Finished exporting design database to file 'pre_syn_map' for 'riscv_top' ...
_CONFIRM_RE = {
    "genus.log": re.compile(r"Finished exporting design database to file 'pre_([A-Za-z0-9_]+)'"),
}

# Latest step a resume may start from, per tool. Genus's fill_outputs requires
# write_regs and write_outputs to have run in the current invocation, so a
# resume that starts after write_regs would finish the tool but fail hammer's
# bookkeeping. The steps from write_regs on cost seconds, so the clamp is cheap.
_RESUME_CEILING = {
    "genus.log": "write_regs",
}


def is_enabled(driver: Optional[Any] = None) -> bool:
    """True unless switched off by env var or config. Defaults to on."""
    env = os.environ.get(ENABLE_ENV_VAR)
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    if driver is not None:
        try:
            val = driver.database.get_setting(ENABLE_SETTING_KEY, nullvalue=None)
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            pass
    return True


def _newest_log(rundir: str, log_name: str) -> Optional[str]:
    """The most recent tool log, accounting for Cadence-style rotation
    (genus.log, genus.log1, genus.log2, ...)."""
    cands = glob.glob(os.path.join(rundir, log_name + "*"))
    cands = [c for c in cands if re.fullmatch(re.escape(log_name) + r"\d*", os.path.basename(c))]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def confirmed_checkpoints(rundir: str, log_name: str = "genus.log") -> List[str]:
    """Step names whose pre_<step> checkpoint the tool confirmed writing, in
    log order, filtered to files still present and non-empty."""
    pat = _CONFIRM_RE.get(log_name)
    log = _newest_log(rundir, log_name)
    if pat is None or log is None:
        return []
    names: List[str] = []
    try:
        with open(log, errors="ignore") as fh:
            for line in fh:
                m = pat.search(line)
                if m and m.group(1) not in names:
                    names.append(m.group(1))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(rundir, "pre_" + n)
        try:
            if os.path.getsize(p) > 0:
                out.append(n)
        except OSError:
            continue
    return out


def _marker_path(rundir: str) -> str:
    return os.path.join(rundir, MARKER_NAME)


def read_marker(rundir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_marker_path(rundir)) as fh:
            return json.load(fh)
    except Exception:
        return None


def write_marker(rundir: str, data: Dict[str, Any]) -> None:
    try:
        os.makedirs(rundir, exist_ok=True)
        with open(_marker_path(rundir), "w") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        # advisory state only; never fail the run over it
        pass


def clean_checkpoints(rundir: str) -> None:
    """Remove stale checkpoint dbs and the marker (inputs changed)."""
    for p in glob.glob(os.path.join(rundir, "pre_*")):
        if re.fullmatch(r"pre_[A-Za-z0-9_]+", os.path.basename(p)) and os.path.isfile(p):
            try:
                os.unlink(p)
            except OSError:
                pass
    try:
        os.unlink(_marker_path(rundir))
    except OSError:
        pass


def _stage_key(driver: Any, stage_tag: str) -> Optional[str]:
    try:
        from hammer.vlsi.pd_cache import _build_cache_key
        return _build_cache_key(driver, stage_tag)
    except Exception:
        return None


def plan_resume(driver: Any, stage_tag: str, rundir: str, output_filename: str,
                log_name: str = "genus.log") -> Optional[Dict[str, Any]]:
    """Decide whether the coming run can resume from a checkpoint.

    Returns ``{"step": <name>, "saved_seconds": <float|None>, "key": <key>}``
    when a trusted checkpoint exists for unchanged inputs, else None (run from
    scratch). Never raises; any doubt means None.
    """
    try:
        if not is_enabled(driver):
            return None
        if os.path.exists(os.path.join(rundir, output_filename)):
            return None  # previous run completed; dep-check/cache own this case
        confirmed = confirmed_checkpoints(rundir, log_name)
        if not confirmed:
            return None
        key = _stage_key(driver, stage_tag)
        marker = read_marker(rundir)
        if key is None or marker is None or marker.get("stage_key") != key:
            # checkpoints came from different inputs (or we can't prove
            # otherwise): they are worthless and could mislead a later run
            clean_checkpoints(rundir)
            return None
        burned = list(marker.get("burned", []))
        # a resume that produced no new confirmed checkpoint made no progress:
        # burn that rung so we step down the ladder instead of looping
        last = marker.get("resumed_from")
        if last is not None and confirmed and confirmed[-1] == last:
            if last not in burned:
                burned.append(last)
        candidates = [c for c in confirmed if c not in burned]
        # The ceiling is positional in the confirmed order, so it holds even
        # when the ceiling step itself has been burned off the ladder.
        ceiling = _RESUME_CEILING.get(log_name)
        if ceiling and ceiling in confirmed:
            allowed = set(confirmed[: confirmed.index(ceiling) + 1])
            candidates = [c for c in candidates if c in allowed]
        if not candidates:
            clean_checkpoints(rundir)
            return None
        step = candidates[-1]
        # measured time of the completed steps, from checkpoint mtimes: the
        # span from the first confirmed boundary to the resume point. This is
        # a floor (the first step's own time isn't bracketed by checkpoints).
        saved: Optional[float] = None
        try:
            t0 = os.path.getmtime(os.path.join(rundir, "pre_" + confirmed[0]))
            t1 = os.path.getmtime(os.path.join(rundir, "pre_" + step))
            saved = max(0.0, t1 - t0)
        except OSError:
            pass
        if burned != marker.get("burned", []):
            marker["burned"] = burned
            write_marker(rundir, marker)
        return {"step": step, "saved_seconds": saved, "key": key}
    except Exception:
        return None


def record_attempt(driver: Any, stage_tag: str, rundir: str,
                   resumed_from: Optional[str]) -> None:
    """Stamp the marker for the attempt that is about to run.

    Keeps the burned ladder when the inputs are unchanged; a new stage key
    starts fresh (the old ladder belonged to different inputs).
    """
    try:
        key = _stage_key(driver, stage_tag)
        if key is None:
            return
        old = read_marker(rundir)
        burned = list(old.get("burned", [])) if old and old.get("stage_key") == key else []
        write_marker(rundir, {
            "stage_key": key,
            "ts": time.time(),
            "resumed_from": resumed_from,
            "burned": burned,
        })
    except Exception:
        pass
