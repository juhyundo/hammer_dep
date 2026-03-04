# Compute a stable fingerprint for Hammer hook lists.
# Used by cli_driver to detect hook changes between runs.

from __future__ import annotations

import hashlib
import importlib
import inspect
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from hammer.vlsi.hooks import HammerToolHookAction


def fingerprint_hooks(hooks: List) -> str:
    """
    SHA256 over source code + structural metadata of a list of HammerToolHookAction.

    Hooks are sorted by (location, target_name) so list ordering doesn't matter.
    """
    h = hashlib.sha256()
    for action in sorted(hooks, key=lambda a: (a.location.name, a.target_name)):
        h.update(action.location.name.encode())
        h.update(b"\0")
        h.update(action.target_name.encode())
        h.update(b"\0")
        if action.step is not None:
            h.update(action.step.name.encode())
            h.update(b"\0")
            try:
                src = inspect.getsource(action.step.func)
                h.update(src.encode())
            except (OSError, TypeError):
                # Fallback for lambdas or C-extension callables
                qname = getattr(action.step.func, "__qualname__", "") + \
                        getattr(action.step.func, "__module__", "")
        h.update(b"\xff")  # separator between actions
    return h.hexdigest()


def fingerprint_tool_module(tool_name: str, stage: str) -> str:
    """
    Hash the get_tool_hooks source of the tool plugin for a given stage.

    stage is the Hammer stage namespace: "synthesis", "par", "drc", "lvs".
    Falls back to hashing the whole plugin file, then to the bare name string.
    """
    if not tool_name:
        return hashlib.sha256(f"{stage}.<unknown>".encode()).hexdigest()
    try:
        mod = importlib.import_module(f"hammer.{stage}.{tool_name}")
        # Prefer hashing only get_tool_hooks to avoid false positives from
        # unrelated changes elsewhere in the plugin file.
        for obj in mod.__dict__.values():
            if isinstance(obj, type) and hasattr(obj, "get_tool_hooks"):
                try:
                    src = inspect.getsource(obj.get_tool_hooks)
                    return hashlib.sha256(src.encode()).hexdigest()
                except OSError:
                    pass
        # Fallback: hash the entire source file
        src_file = inspect.getfile(mod)
        with open(src_file, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return hashlib.sha256(f"{stage}.{tool_name}".encode()).hexdigest()


def fingerprint_stage_hooks(tech_hooks: List, user_hooks: List,
                             tool_name: str, stage: str) -> str:
    """
    Combine all three hook sources into one fingerprint for a stage.

    :param tech_hooks: Hooks from HammerTechnology.get_tech_*_hooks()
    :param user_hooks: User/extra hooks passed via create_*_action()
    :param tool_name:  Tool plugin name (e.g. "genus", "innovus")
    :param stage:      Stage namespace (e.g. "synthesis", "par")
    """
    h = hashlib.sha256()
    h.update(fingerprint_hooks(tech_hooks).encode())
    h.update(fingerprint_hooks(user_hooks).encode())
    h.update(fingerprint_tool_module(tool_name, stage).encode())
    return h.hexdigest()
