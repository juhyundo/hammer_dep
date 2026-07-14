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
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MARKER_NAME = ".substep_resume.json"
ENABLE_ENV_VAR = "HAMMER_SUBSTEP_RESUME"
ENABLE_SETTING_KEY = "vlsi.substep_resume.enabled"
DB_ENABLE_ENV_VAR = "HAMMER_DB_CHECKPOINTS"
DB_ENABLE_SETTING_KEY = "vlsi.substep_resume.db_checkpoints"

# Tool-confirmed checkpoint write, per tool log.
#   genus prints an explicit completion line per export.
#   innovus prints when a write STARTS; completion is inferred (see
#   confirmed_checkpoints): a later write starting means the earlier one
#   finished, and the newest write is trusted only if the `latest` symlink
#   (repointed by the script right after write_db returns) points at it.
_CONFIRM_RE = {
    "genus.log": re.compile(r"Finished exporting design database to file 'pre_([A-Za-z0-9_]+)'"),
    "innovus.log": re.compile(r"Writing Binary DB to pre_([A-Za-z0-9_]+)/"),
}
_INFER_COMPLETION = {"innovus.log"}

# Latest step a resume may start from, per tool. Both genus and innovus
# fill_outputs require write_regs and the write steps after it to have run in
# the current invocation (ran_write_regs / ran_write_design / ran_write_ilm),
# so a later resume would finish the tool but fail hammer's bookkeeping. The
# steps from write_regs on are cheap.
_RESUME_CEILING = {
    "genus.log": "write_regs",
    "innovus.log": "write_regs",
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


def _checkpoint_present(rundir: str, step: str) -> bool:
    """A checkpoint exists with content: a non-empty file (genus writes a
    single db file) or a non-empty directory (innovus writes a db dir)."""
    p = os.path.join(rundir, "pre_" + step)
    try:
        if os.path.isdir(p):
            return bool(os.listdir(p))
        return os.path.getsize(p) > 0
    except OSError:
        return False


def confirmed_checkpoints(rundir: str, log_name: str = "genus.log") -> List[str]:
    """Step names whose pre_<step> checkpoint the tool confirmed writing, in
    log order, filtered to checkpoints still present with content.

    For tools that only announce the START of a db write (innovus), completion
    is inferred: every write except the last is complete because a later write
    began after it; the last one counts only if the `latest` symlink points at
    it, since the generated script repoints `latest` immediately after
    write_db returns.

    Confirmations are collected across ALL log rotations, not just the newest:
    a resume attempt that dies loading its checkpoint confirms nothing in its
    own log, but the checkpoints proven by earlier attempts are still on disk
    and still valid (a stage-key change deletes them, so presence plus the
    marker key check is sufficient). The result is ordered by checkpoint
    mtime, oldest first, which is completion order."""
    pat = _CONFIRM_RE.get(log_name)
    if pat is None:
        return []
    cands = glob.glob(os.path.join(rundir, log_name + "*"))
    cands = [c for c in cands
             if re.fullmatch(re.escape(log_name) + r"\d*", os.path.basename(c))]
    if not cands:
        return []
    target = None
    try:
        target = os.path.basename(os.readlink(os.path.join(rundir, "latest")))
    except OSError:
        pass
    def _log_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0
    announced: List[str] = []  # first-seen announced order, oldest log first
    for log in sorted(cands, key=_log_mtime):
        names: List[str] = []
        try:
            with open(log, errors="ignore") as fh:
                for line in fh:
                    m = pat.search(line)
                    if m and m.group(1) not in names:
                        names.append(m.group(1))
        except OSError:
            continue
        # the drop-the-last-announced rule applies per attempt (per log)
        if names and log_name in _INFER_COMPLETION and target != "pre_" + names[-1]:
            names = names[:-1]
        for n in names:
            if n not in announced:
                announced.append(n)
    present = [n for n in announced if _checkpoint_present(rundir, n)]

    # completion order: checkpoint mtime, announced order as the tiebreak
    # (synthetic same-second writes, coarse filesystems)
    def _ck_key(n: str):
        try:
            mt = os.path.getmtime(os.path.join(rundir, "pre_" + n))
        except OSError:
            mt = 0.0
        return (mt, announced.index(n))
    return sorted(present, key=_ck_key)


def announced_order(rundir: str, log_name: str = "genus.log") -> List[str]:
    """Step names in first-announced order across all log rotations.

    Once any attempt announced a step's boundary, its position here reflects
    true tool step order (a full run announces everything in order; partial
    runs re-announce prefixes). Used for the resume ceiling, where mtime
    order is wrong for mixed-generation rundirs: an old completed run's
    pre_write_regs has an EARLIER mtime than a fresh attempt's pre_clock_tree
    even though write_regs is a later step."""
    pat = _CONFIRM_RE.get(log_name)
    if pat is None:
        return []
    cands = glob.glob(os.path.join(rundir, log_name + "*"))
    cands = [c for c in cands
             if re.fullmatch(re.escape(log_name) + r"\d*", os.path.basename(c))]

    def _log_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0
    announced: List[str] = []
    for log in sorted(cands, key=_log_mtime):
        try:
            with open(log, errors="ignore") as fh:
                for line in fh:
                    m = pat.search(line)
                    if m and m.group(1) not in announced:
                        announced.append(m.group(1))
        except OSError:
            continue
    return announced



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
        if not re.fullmatch(r"pre_[A-Za-z0-9_]+", os.path.basename(p)):
            continue
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
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


def _db_enabled(driver: Optional[Any] = None) -> bool:
    env = os.environ.get(DB_ENABLE_ENV_VAR)
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    if driver is not None:
        try:
            val = driver.database.get_setting(DB_ENABLE_SETTING_KEY, nullvalue=None)
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            pass
    return True


def _provenance(driver: Any) -> dict:
    project = os.environ.get("HAMMER_PD_PROJECT")
    if not project:
        try:
            project = driver.database.get_setting("vlsi.pd_cache.project", nullvalue=None)
        except Exception:
            project = None
    return {
        "triggering_user": os.environ.get("HAMMER_AIRFLOW_TRIGGER_USER"),
        "dag_id": os.environ.get("HAMMER_AIRFLOW_DAG_ID"),
        "dag_run_id": os.environ.get("HAMMER_AIRFLOW_RUN_ID"),
        "workspace": os.environ.get("HAMMER_WORKSPACE"),
        "design": os.environ.get("HAMMER_AIRFLOW_DESIGN"),
        "project": project,
    }


def _log_info(driver: Any, msg: str) -> None:
    try:
        driver.log.info(msg)
    except Exception:
        pass


def push_checkpoint_db(driver: Any, stage_tag: str, rundir: str,
                       log_name: str = "genus.log",
                       module: Optional[str] = None) -> Optional[str]:
    """After a failed or paused run, upload the newest trusted checkpoint so
    another machine or a fresh checkout can resume this stage. Never raises;
    returns the pushed step name or None."""
    try:
        if not is_enabled(driver) or not _db_enabled(driver):
            return None
        key = _stage_key(driver, stage_tag)
        if key is None:
            return None
        marker = read_marker(rundir)
        if marker is None or marker.get("stage_key") != key:
            return None
        confirmed = confirmed_checkpoints(rundir, log_name)
        # a checkpoint past the resume ceiling can never seed a resume;
        # clamp by announced (step) order, not mtime position
        ceiling = _RESUME_CEILING.get(log_name)
        announced = announced_order(rundir, log_name)
        if ceiling and ceiling in announced:
            allowed = set(announced[: announced.index(ceiling) + 1])
            confirmed = [c for c in confirmed if c in allowed]
        if not confirmed:
            return None
        step = confirmed[-1]
        path = os.path.join(rundir, "pre_" + step)
        from hammer.vlsi import pd_store
        from pathlib import Path
        size = pd_store.store_checkpoint(key, stage_tag, step, Path(path),
                                         module=module, **_provenance(driver))
        _log_info(driver, f"Pushed checkpoint pre_{step} ({size / 1e6:.1f} MB "
                          "compressed) to the database for cross-machine resume.")
        return step
    except Exception:
        if os.environ.get("HAMMER_CHECKPOINT_DEBUG"):
            import traceback
            traceback.print_exc()
        return None


def clear_checkpoint_db(driver: Any, stage_tag: str,
                        rundir: Optional[str] = None) -> int:
    """Drop this stage's database checkpoints once it commits successfully.

    Clears the current stage key, plus every key in this rundir's attempt
    lineage (the marker's key_history): the usual fix-the-config-then-succeed
    flow sweeps the rows its own broken predecessors pushed, while a teammate
    debugging a different config of the same design keeps their row (their
    keys are not in this rundir's lineage). Never raises; returns rows
    deleted."""
    try:
        if not _db_enabled(driver):
            return 0
        key = _stage_key(driver, stage_tag)
        if key is None:
            return 0
        keys = [key]
        if rundir:
            marker = read_marker(rundir)
            if marker:
                keys += [k for k in marker.get("key_history", []) if k not in keys]
        from hammer.vlsi import pd_store
        n = 0
        for k in keys:
            n += pd_store.delete_checkpoints(stage_key=k)
        if n:
            _log_info(driver, f"Cleared {n} database checkpoint(s) for the completed stage.")
        return n
    except Exception:
        if os.environ.get("HAMMER_CHECKPOINT_DEBUG"):
            import traceback
            traceback.print_exc()
        return 0


def _db_fallback_plan(driver: Any, stage_tag: str, rundir: str) -> Optional[Dict[str, Any]]:
    """No usable local checkpoint: try the database. Downloads the newest
    checkpoint stored for this exact stage key and materializes it into the
    rundir. The row's existence is its trust: it was log-confirmed and
    key-matched when pushed. Never raises."""
    try:
        if not _db_enabled(driver):
            return None
        key = _stage_key(driver, stage_tag)
        if key is None:
            return None
        from hammer.vlsi import pd_store
        rec = pd_store.fetch_checkpoint(key)
        if rec is None:
            return None
        step = rec["step"]
        # anti-loop: if the previous attempt already resumed from this very
        # checkpoint and confirmed nothing new, don't fetch it again
        marker = read_marker(rundir)
        if marker is not None and marker.get("stage_key") == key:
            if step in marker.get("burned", []) or step == marker.get("resumed_from"):
                return None
        from pathlib import Path
        pd_store.materialize_checkpoint(rec, Path(rundir))
        _log_info(driver, f"Fetched checkpoint pre_{step} "
                          f"({rec['size_bytes'] / 1e6:.1f} MB) from the database.")
        return {"step": step, "saved_seconds": None, "key": key, "source": "database"}
    except Exception:
        return None


def ensure_step_checkpoint(driver: Any, stage_tag: str, rundir: str, step_name: str,
                           log_name: str = "genus.log"):
    """Validate a user-chosen start step: its pre_<step> checkpoint must exist
    locally or be fetchable from the database. Returns (ok, detail); on
    failure the detail lists what IS available."""
    try:
        if _checkpoint_present(rundir, step_name):
            return True, "local"
        key = _stage_key(driver, stage_tag)
        if _db_enabled(driver) and key is not None:
            from hammer.vlsi import pd_store
            rec = pd_store.fetch_checkpoint(key, step=step_name)
            if rec is not None:
                from pathlib import Path
                pd_store.materialize_checkpoint(rec, Path(rundir))
                return True, "database"
        local = sorted(os.path.basename(p)[len("pre_"):]
                       for p in glob.glob(os.path.join(rundir, "pre_*"))
                       if _checkpoint_present(rundir, os.path.basename(p)[len("pre_"):]))
        indb = []
        if _db_enabled(driver) and key is not None:
            try:
                from hammer.vlsi import pd_store
                indb = [r["step"] for r in pd_store.find_checkpoints(stage_key=key)]
            except Exception:
                indb = []
        return False, (f"no checkpoint exists for step '{step_name}'. "
                       f"Available locally: {', '.join(local) or 'none'}. "
                       f"Available in the database for these inputs: "
                       f"{', '.join(indb) or 'none'}.")
    except Exception as exc:
        return False, f"checkpoint lookup failed: {exc}"


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
        confirmed = confirmed_checkpoints(rundir, log_name)
        if not confirmed:
            return _db_fallback_plan(driver, stage_tag, rundir)
        # A present output json only means "completed" if it is newer than the
        # newest confirmed checkpoint. An older one is a leftover from an
        # earlier successful run that a later (killed) attempt superseded --
        # common when re-running a stage in a long-lived build dir.
        out_path = os.path.join(rundir, output_filename)
        if os.path.exists(out_path):
            try:
                newest_ck = max(os.path.getmtime(os.path.join(rundir, "pre_" + c))
                                for c in confirmed)
                if os.path.getmtime(out_path) > newest_ck:
                    return None  # genuinely completed; dep-check/cache own this
            except OSError:
                return None
        key = _stage_key(driver, stage_tag)
        marker = read_marker(rundir)
        if key is None or marker is None or marker.get("stage_key") != key:
            # checkpoints came from different inputs (or we can't prove
            # otherwise): they are worthless and could mislead a later run.
            # The database may still hold one pushed for the CURRENT inputs
            # (e.g. by another machine), so check it before going scratch.
            clean_checkpoints(rundir)
            return _db_fallback_plan(driver, stage_tag, rundir)
        burned = list(marker.get("burned", []))
        # a resume that produced no new confirmed checkpoint made no progress:
        # burn that rung so we step down the ladder instead of looping
        last = marker.get("resumed_from")
        if last is not None and confirmed and confirmed[-1] == last:
            if last not in burned:
                burned.append(last)
        candidates = [c for c in confirmed if c not in burned]
        # The ceiling is positional in ANNOUNCED (step) order, which survives
        # both a burned ceiling step and mixed-generation rundirs, where an
        # old run's late-step checkpoint has an earlier mtime than a fresh
        # attempt's early-step one.
        ceiling = _RESUME_CEILING.get(log_name)
        announced = announced_order(rundir, log_name)
        if ceiling and ceiling in announced:
            allowed = set(announced[: announced.index(ceiling) + 1])
            candidates = [c for c in candidates if c in allowed]
        if not candidates:
            clean_checkpoints(rundir)
            return _db_fallback_plan(driver, stage_tag, rundir)
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
        # lineage of config keys this rundir has attempted: lets a later
        # success clear the database rows its own earlier (differently
        # configured) attempts pushed, without touching anyone else's
        history = list(old.get("key_history", [])) if old else []
        if old and old.get("stage_key") and old["stage_key"] != key \
                and old["stage_key"] not in history:
            history.append(old["stage_key"])
        write_marker(rundir, {
            "stage_key": key,
            "ts": time.time(),
            "resumed_from": resumed_from,
            "burned": burned,
            "key_history": history[-10:],
        })
    except Exception:
        pass
