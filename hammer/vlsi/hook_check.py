# Compute a stable fingerprint for Hammer hook lists.
# Used by cli_driver to detect hook changes between runs.

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import textwrap
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from hammer.vlsi.hooks import HammerToolHookAction


def _ast_hash(func) -> str:
    """
    SHA256 of a function's AST.
    This ignores comments, docstrings, and formatting.
    """
    src = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(src)
    # Strip column/line formatting. AST already captures logical indents.
    for node in ast.walk(tree):
        for attr in ("lineno", "col_offset", "end_lineno", "end_col_offset",
                        "type_comment"):
            node.__dict__.pop(attr, None)
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()


def fingerprint_hooks(hooks: List) -> str:
    """
    SHA256 over the AST + location, target_name, step of a list of HammerToolHookAction.
    """
    h = hashlib.sha256()
    for action in hooks:
        h.update(action.location.name.encode())
        h.update(b"\0")
        h.update(action.target_name.encode())
        h.update(b"\0")
        if action.step is not None:
            h.update(action.step.name.encode())
            h.update(b"\0")
            h.update(_ast_hash(action.step.func).encode())
        h.update(b"\xff")  # separator between actions
    return h.hexdigest()


def fingerprint_tool_module(tool_name: str, stage: str) -> str:
    """
    Hash the AST of get_tool_hooks from the tool plugin.
    tool_name is the full module path (e.g. "hammer.synthesis.genus").
    Raises if the module cannot be imported.
    """
    if not tool_name:
        return hashlib.sha256(f"{stage}.<unknown>".encode()).hexdigest()
    
    mod = importlib.import_module(tool_name)
    tool_class = getattr(mod, "tool")
    return _ast_hash(tool_class.get_tool_hooks)


def fingerprint_stage_hooks(tech_hooks: List, user_hooks: List,
                             tool_name: str, stage: str) -> str:
    """
    Combine all three hook sources into one fingerprint for a stage.

    :param tech_hooks: Hooks from HammerTechnology.get_tech_*_hooks()
    :param user_hooks: User/extra hooks passed via create_*_action()
    :param tool_name:  Full tool module path (e.g. "hammer.synthesis.genus")
    :param stage:      Stage namespace (e.g. "synthesis", "par")
    """
    h = hashlib.sha256()
    h.update(fingerprint_hooks(tech_hooks).encode())
    h.update(fingerprint_hooks(user_hooks).encode())
    h.update(fingerprint_tool_module(tool_name, stage).encode())
    return h.hexdigest()
