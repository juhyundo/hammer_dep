#  Generate a stable RTL fingerprint from the elaborated SystemVerilog design.
#
#  File order is significant in RTL compilation, so every input is passed to
#  slang as ONE compilation unit (`--single-unit`), in the order given.
#
#  The canonical form is slang's own `--ast-json` output.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

#  The fingerprint is a function of slang's JSON shape:
#  different version of slang can silently move every stored hash and mass-invalidate the
#  cache.
SLANG_VERSION = "11.0"


class RtlParseError(Exception):
    """Raised when slang reports an error while compiling the RTL."""

    def __init__(self, path: str, report: str) -> None:
        super().__init__(f"failed to parse {path}:\n{report}")
        self.path = path
        self.report = report


class SlangNotFound(Exception):
    """Raised when the pinned slang binary cannot be located."""
@dataclass(frozen=True)
class DesignUnit:
    """
    The elaborated design and the hash of its canonical form.

    One of these per run.  It stays a list in the return type because both
    callers unpack a two-tuple (``pd_store.compute_rtl_fingerprint``,
    ``cli_driver``) and neither reads past the hash.
    """

    key: str          # always "design"
    sha256: str       # identical to the overall fingerprint
    files: str        # source file(s) that contributed; NOT part of the hash
    tokens: int       # canonical node count; NOT part of the hash


# ── Locating slang ──────────────────────────────────────────────────────────
def _repo_bundled_slang() -> Optional[str]:
    """The copy scripts/uv_setup.sh drops in the checkout, if present."""
    here = os.path.dirname(os.path.realpath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    candidate = os.path.join(repo, "tools", "slang", "slang")
    return candidate if os.path.isfile(candidate) else None


def slang_binary() -> str:
    """
    Absolute path to the slang executable.

    ``$SLANG_BIN`` wins so a site can pin its own build, then PATH, then the copy
    bundled in the checkout.
    """
    env = os.environ.get("SLANG_BIN")
    if env:
        if not os.path.isfile(env):
            raise SlangNotFound(f"$SLANG_BIN={env!r} is not a file. {_INSTALL_HINT}")
        return env
    found = shutil.which("slang") or _repo_bundled_slang()
    if not found:
        raise SlangNotFound(f"slang {SLANG_VERSION} not found on PATH. {_INSTALL_HINT}")
    return found


def slang_version(binary: Optional[str] = None) -> str:
    """The ``MAJOR.MINOR`` version the binary reports."""
    out = subprocess.run([binary or slang_binary(), "--version"],
                         capture_output=True, text=True).stdout
    # "slang version 11.0.0+7ddf4059f"
    m = re.search(r"(\d+)\.(\d+)", out)
    return f"{m.group(1)}.{m.group(2)}" if m else out.strip()


def _checked_binary() -> str:
    """Locate slang and refuse a version whose JSON shape we have not pinned."""
    binary = slang_binary()
    have = slang_version(binary)
    if have != SLANG_VERSION:
        raise SlangNotFound(
            f"slang {have} found at {binary}, but the fingerprint is pinned to "
            f"{SLANG_VERSION}. A different slang can change the AST JSON and move "
            f"every stored fingerprint. {_INSTALL_HINT}")
    return binary


# ── Running slang ───────────────────────────────────────────────────────────

def _run_slang(paths: Sequence[str],
               include_dirs: Sequence[str],
               defines: Sequence[str],
               top_module: Optional[str]) -> Dict[str, Any]:
    """
    Compile the inputs and return slang's elaborated AST as parsed JSON.

    ``--ignore-unknown-modules`` is what lets a design instantiating a tech macro
    or SRAM be fingerprinted at all: those are resolved from liberty by the real
    tools and have no definition among ``input_files``, so slang leaves them as
    black boxes and elaboration continues.

    ``--ast-json-detailed-types`` expands types structurally instead of printing
    a typedef by its alias name, which is what lets widening a shared typedef
    reach the modules that use it.
    """
    for path in paths:
        if not os.path.isfile(path):
            raise FileNotFoundError(2, "No such file or directory", path)

    binary = _checked_binary()
    fd, out_path = tempfile.mkstemp(prefix="rtl_ast.", suffix=".json")
    os.close(fd)
    try:
        # --diag-abs-paths: diagnostics land in build logs and in RtlParseError,
        # where slang's default CWD-relative "../../../../private/var/..." is
        # unreadable and depends on where the run started.
        cmd = [binary, "-q", "--single-unit", "--ignore-unknown-modules",
               "--diag-abs-paths",
               "--ast-json-detailed-types", "--ast-json-source-info",
               "--ast-json", out_path]
        for d in include_dirs:
            cmd += ["-I", d]
        for d in defines:
            cmd += ["-D", d]
        if top_module:
            cmd += ["--top", top_module]
        cmd += list(paths)

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            # slang still writes a JSON file on error; it describes a design that
            # did not compile, so it must not be hashed.  Its own diagnostics
            # already carry file:line, so they are the whole report.
            report = (proc.stderr or "") + (proc.stdout or "")
            blamed = _blamed_file(report, paths)
            raise RtlParseError(blamed, _explain(report))
        with open(out_path, "r", encoding="utf-8") as f:
            return json.load(f)
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


_EXPLAINED: Tuple[Tuple[str, str], ...] = (
    ("-Wduplicate-definition",
     "the same module is declared more than once across the input files.\n"
     "Fix: remove the duplicate module, or drop the redundant file from\n"
     "synthesis.inputs.input_files."),
)


def _explain(report: str) -> str:
    """Append a fingerprint-specific note to a slang diagnostic we recognize."""
    text = report.strip()
    for marker, note in _EXPLAINED:
        if marker in report:
            return f"{text}\n\nrtl_check: {note}"
    return text


_DIAG_FILE = re.compile(r"^\s*(\S+?):\d+:\d+:", re.MULTILINE)


def _blamed_file(report: str, paths: Sequence[str]) -> str:
    """The first file slang's diagnostics point at, for the exception message."""
    m = _DIAG_FILE.search(report)
    if m:
        return os.path.realpath(m.group(1))
    return paths[0] if paths else "<none>"


# ── Canonical form ──────────────────────────────────────────────────────────

#  slang emits raw heap pointers, both as an "addr" key and as an identity
#  prefix inside cross-reference strings ("type": "6338699662056 nib_t").  They
#  differ on every run, so nothing address-derived should survive into the hash.
_ADDR_PREFIX = re.compile(r"^\d+ ")

#  Dropped before hashing.  Source info would make the fingerprint depend on file
#  layout and even on the working directory (slang emits a CWD-relative path).  
#  It is read for the report first, in _contributing_files.
#
#  Matched by prefix, not by an exact set: slang emits a plain source_file on
#  symbols but source_file_start / source_file_end (and the matching line and
#  column pairs) on anything carrying a range, and listing them one by one meant
#  every range-bearing node leaked its filename into the hash.
_VOLATILE_PREFIX = "source_"


def _is_volatile(key: str) -> bool:
    return key == "addr" or key.startswith(_VOLATILE_PREFIX)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _index_by_addr(node: Any, index: Dict[str, Any]) -> None:
    """Map every node's address to the node, so references can be resolved."""
    if isinstance(node, dict):
        addr = node.get("addr")
        if addr is not None:
            index.setdefault(str(addr), node)
        for value in node.values():
            _index_by_addr(value, index)
    elif isinstance(node, list):
        for value in node:
            _index_by_addr(value, index)


def _is_transparent_block(node: Any) -> bool:
    """
    True for a ``begin``/``end`` that carries no meaning.

    Only an *unnamed*, *sequential* block wrapping a *single* statement is pure
    punctuation -- ``if (x) y <= 1;`` and ``begin if (x) begin y <= 1; end end``
    must fingerprint the same.  Everything else stays: a named block can be
    referenced hierarchically, ``fork``/``join`` changes execution semantics, and
    a multi-statement block has a StatementList body whose removal would splice
    those statements into the parent and collide with a different nesting.
    """
    return (isinstance(node, dict)
            and node.get("kind") == "Block"
            and node.get("blockKind") == "Sequential"
            and not node.get("name")
            and isinstance(node.get("body"), dict)
            and node["body"].get("kind") != "StatementList")


def _canon(node: Any, index: Dict[str, Any], resolving: Set[str]) -> Any:
    """
    Canonicalize a JSON subtree for hashing.

    Strips volatile keys, folds transparent blocks, and inlines type references.

    Nothing here interprets the *shape* of a node beyond those three rules, and
    that is deliberate.  An earlier version special-cased ``Instance`` so each
    module could be hashed as its own unit, which meant assuming an instance's
    ``body`` is always a dict -- but slang emits the full body only for the first
    instance of a module and an ``"<addr> <name>"`` reference for every later
    one, so any design instantiating the same module twice crashed.  Treating a
    reference as just another string makes that whole class of bug impossible.
    """
    if isinstance(node, list):
        return [_canon(v, index, resolving) for v in node]
    if isinstance(node, str):
        return _ADDR_PREFIX.sub("", node)
    if not isinstance(node, dict):
        return node

    if _is_transparent_block(node):
        return _canon(node["body"], index, resolving)

    out: Dict[str, Any] = {}
    for key, value in node.items():
        if _is_volatile(key):
            continue
        if key == "type" and isinstance(value, str):
            out[key] = _resolve_type(value, index, resolving)
            continue
        out[key] = _canon(value, index, resolving)
    return out


def _resolve_type(ref: str, index: Dict[str, Any], resolving: Set[str]) -> Any:
    """
    Inline a ``"<addr> <name>"`` type reference.

    A typedef reaches the hash only through the signals declared with it: the use
    site prints the alias name, which is identical before and after the alias is
    widened.  Following the address to the actual definition is what makes
    ``typedef logic [3:0] nib_t`` -> ``[7:0]`` land in every module that uses it,
    while an alias nothing references stays invisible -- dead code costs no
    re-run.  ``resolving`` breaks reference cycles.
    """
    m = _ADDR_PREFIX.match(ref)
    if not m:
        return ref
    addr = m.group(0).strip()
    target = index.get(addr)
    if target is None or addr in resolving:
        return _ADDR_PREFIX.sub("", ref)
    resolving.add(addr)
    try:
        return _canon(target, index, resolving)
    finally:
        resolving.discard(addr)


def _count(node: Any) -> int:
    """Node count of a canonical form, for the human-readable report."""
    if isinstance(node, dict):
        return 1 + sum(_count(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count(v) for v in node)
    return 0


def _contributing_files(node: Any, out: Set[str]) -> None:
    """
    Every source file the elaborated design actually came from.

    Read off the raw document, before canonicalization strips the source keys.
    Purely for the report and for _warn_uncovered; never reaches a hash.
    """
    if isinstance(node, dict):
        src = node.get("source_file")
        if src:
            out.add(os.path.realpath(src))
        for value in node.values():
            _contributing_files(value, out)
    elif isinstance(node, list):
        for value in node:
            _contributing_files(value, out)


# ── Digesting ───────────────────────────────────────────────────────────────

def digest_units(paths: Sequence[str],
                 include_dirs: Sequence[str] = (),
                 defines: Sequence[str] = (),
                 top_module: Optional[str] = None) -> Tuple[str, List[DesignUnit]]:
    """
    Compile every input file and hash each elaborated design unit separately.

    All inputs form ONE compilation unit, in the order given, so macros cross
    file boundaries exactly as they do under VCS ``-mfcu`` or a single Genus
    ``read_hdl -sv``.  That makes the order of ``paths`` significant: a macro has
    to be defined by an earlier file to be visible in a later one.

    :param paths: RTL source files (.v/.sv), in the order the tools see them.
        Header fragments (.vh/.svh) must NOT be listed here: a listed header is
        compiled as a source in its own right rather than inlined at its `include
        site, and a standalone fragment generally is not parsable on its own.
    :param include_dirs: extra `include search directories.
    :param defines: predefined macros, each "NAME" or "NAME=value".
    :param top_module: elaborate from this module, as synthesis does.  A module
        the top never instantiates then contributes nothing -- editing dead RTL
        costs no re-run, exactly as Genus would never synthesize it.  When
        omitted slang infers every uninstantiated module as a top, which is a
        legitimately wider set, so the two modes give different fingerprints.
    :return: (overall fingerprint, per-unit digests sorted by key)
    :raises FileNotFoundError: an input file is missing.
    :raises SlangNotFound: the pinned slang binary is missing or the wrong version.
    :raises RtlParseError: slang reported a parse or elaboration error.
    """
    # Order-preserving dedup: the caller's order is what the tools see, and
    # keeping the first occurrence is what makes fp(x, y, x) == fp(x, y).
    real_paths: List[str] = []
    seen_paths: Set[str] = set()
    for p in paths:
        real = os.path.realpath(p)
        if real not in seen_paths:
            seen_paths.add(real)
            real_paths.append(real)

    doc = _run_slang(real_paths, include_dirs, defines, top_module)

    index: Dict[str, Any] = {}
    _index_by_addr(doc, index)

    # The CompilationUnit member holds file-scope declarations -- typedefs,
    # parameters, imports -- whether or not anything uses them, so hashing it
    # would make editing an unused declaration force a re-run.  Excluding it is
    # what keeps dead code free; a *used* typedef still lands, because
    # _resolve_type follows the reference from every signal declared with it.
    members = [m for m in doc.get("design", {}).get("members", [])
               if not (isinstance(m, dict) and m.get("kind") == "CompilationUnit")]

    # `definitions` carries the per-module settings that live outside the design
    # tree: `celldefine, `unconnected_drive, default net type and lifetime.
    payload = {"design": _canon(members, index, set()),
               "definitions": _canon(doc.get("definitions", []), index, set())}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    overall = sha256_hex(text.encode("utf-8"))

    covered: Set[str] = set()
    _contributing_files(doc.get("design", {}), covered)
    _warn_uncovered(real_paths, covered)

    unit = DesignUnit(key="design", sha256=overall,
                      files=",".join(sorted(covered)) or (real_paths[0] if real_paths else ""),
                      tokens=_count(payload))
    return overall, [unit]


def _warn_uncovered(paths: Sequence[str], covered: Set[str]) -> None:
    """
    Warn about listed files that contributed nothing to the fingerprint.

    Elaborating from a top module is what makes a dead module stop forcing a
    re-run, which is the point.  The same mechanism means a stale or misspelled
    ``top_module`` silently shrinks coverage while still returning a
    healthy-looking hash -- so say so.  Diagnostic only: this never raises and
    never touches a hash.
    """
    missing = [p for p in paths if p not in covered]
    if missing and len(missing) != len(paths):
        print(f"rtl_check: {len(missing)} of {len(paths)} input files contributed no "
              f"design units; check synthesis.inputs.top_module reaches them:",
              file=sys.stderr)
        for p in missing:
            print(f"  {p}", file=sys.stderr)


def digest_files(paths: Sequence[str],
                 include_dirs: Sequence[str] = (),
                 defines: Sequence[str] = (),
                 top_module: Optional[str] = None) -> Tuple[str, List[DesignUnit]]:
    """Backwards-compatible alias for :func:`digest_units`."""
    return digest_units(paths, include_dirs=include_dirs, defines=defines,
                        top_module=top_module)


# ── Output ──────────────────────────────────────────────────────────────────

def format_fingerprint(overall: str, units: Sequence[DesignUnit]) -> str:
    """
    The human-readable report: the fingerprint, then the files behind it.

    The file list is what makes a surprising hash diagnosable -- if a file you
    expected is missing, ``top_module`` does not reach it.
    """
    lines = [f"overall_sha256 {overall}\n"]
    for u in units:
        lines.append(f"nodes {u.tokens}\n")
        for f in u.files.split(",") if u.files else []:
            lines.append(f"  {f}\n")
    return "".join(lines)


def write_if_changed(out_path: str, contents: str) -> bool:
    """
    Write contents to out_path, but only update the file if contents changed.
    Returns True if the file was updated, False if left untouched.
    """
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            existing = f.read()
        if existing == contents:
            return False
    except FileNotFoundError:
        pass

    out_dir = os.path.dirname(os.path.realpath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".rtl_fingerprint.", dir=out_dir, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(contents)
        os.replace(tmp_path, out_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute an RTL fingerprint from the elaborated SystemVerilog design.")
    parser.add_argument("--out", required=True, help="Output fingerprint file path.")
    parser.add_argument("-I", "--include-dir", action="append", default=[],
                        metavar="DIR", help="Extra `include search directory (repeatable).")
    parser.add_argument("-D", "--define", action="append", default=[],
                        metavar="NAME[=VALUE]", help="Predefined macro (repeatable).")
    parser.add_argument("--top", default=None, metavar="MODULE",
                        help="Elaborate from this top module, as synthesis does.")
    parser.add_argument("inputs", nargs="+", help="Input RTL files (.v/.sv).")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        overall, units = digest_units(args.inputs,
                                      include_dirs=args.include_dir,
                                      defines=args.define,
                                      top_module=args.top)
    except RtlParseError as e:
        print(e.report, file=sys.stderr)
        return 1
    except SlangNotFound as e:
        print(f"rtl_check: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"RTL file not found: {e.filename}", file=sys.stderr)
        return 1

    write_if_changed(args.out, format_fingerprint(overall, units))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
