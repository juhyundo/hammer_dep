#  Generate a stable RTL fingerprint from the SystemVerilog syntax tree.
#
#  File order is significant in RTL compilation. 
#  Hence, Every RTL input is parsed as ONE compilation unit, in the order given and digested by VCS / Genus.

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:
    import pyslang
    from pyslang import ast
    from pyslang.parsing import PreprocessorOptions, Token, TriviaKind
    from pyslang.syntax import SyntaxTree
except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject
    raise ImportError(
        "rtl_check requires the 'pyslang' package to compute RTL fingerprints. "
        "Install it with `pip install pyslang` (it is a declared hammer dependency)."
    ) from exc


class RtlParseError(Exception):
    """Raised when slang reports an error diagnostic while parsing RTL."""

    def __init__(self, path: str, report: str) -> None:
        super().__init__(f"failed to parse {path}:\n{report}")
        self.path = path
        self.report = report


@dataclass(frozen=True)
class DesignUnit:
    """One elaborated design unit and the hash of its canonical form."""

    key: str          # "module:gcd", "$directives:1a2b...", "$unit:1a2b..."
    sha256: str
    files: str        # source file(s) the unit was found in; NOT part of the hash
    tokens: int


# Preprocessor directives that change how the design elaborates or simulates.
# These survive as trivia (the preprocessor consumes them, so they are not part
# of the token stream) and would otherwise be invisible to the fingerprint.
_SEMANTIC_DIRECTIVES: Set[str] = {
    "TimeScaleDirective",
    "DefaultNetTypeDirective",
    "UnconnectedDriveDirective",
    "NoUnconnectedDriveDirective",
    "CellDefineDirective",
    "EndCellDefineDirective",
    "ResetAllDirective",
    "PragmaDirective",
    "BeginKeywordsDirective",
    "EndKeywordsDirective",
    "DefaultDecayTimeDirective",
    "DefaultTriregStrengthDirective",
}

# Directives deliberately NOT hashed: `define / `undef / `include and the whole `ifdef family to avoid 
# unused macros being counted in hash. Post-preprocessing token stream picks up the used macros.

# Literal token kinds whose text is normalized before hashing, so that
# ex) 16'hA_b and 16'hab hash identically.
_NUMERIC_TOKENS = {
    "IntegerLiteral",
    "RealLiteral",
    "TimeLiteral",
    "UnbasedUnsizedLiteral",
}

# Fields that tell two nodes of the same kind apart but appear nowhere among the
# node's children, so the pre-order walk cannot see them.  Without these, a
# blocking/non-blocking swap, a flipped clock edge, always_comb becoming
# always_latch, and casez/unique all hash identically to the original.
#
# Extend table when another such field exist.
_DISCRIMINATORS: Dict[str, Tuple[str, ...]] = {
    "ProceduralBlockSymbol": ("procedureKind",),      # always_comb vs always_latch
    "SignalEventControl":    ("edge",),               # posedge vs negedge
    "AssignmentExpression":  ("isNonBlocking",),      # x <= y vs x = y
    "CaseStatement":         ("condition", "check"),  # casez/casex, unique/priority
    "ConditionalStatement":  ("check",),              # unique if / priority if
}

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Parsing ─────────────────────────────────────────────────────────────────

def _build_context(paths: Sequence[str],
                   include_dirs: Sequence[str] = (),
                   defines: Sequence[str] = ()) -> Tuple[Any, Any]:
    """
    Build the SourceManager + option Bag shared by every file in one run.

    The include search path is the set of directories containing the input
    files, plus any explicitly supplied ``include_dirs``.
    """
    sm = pyslang.SourceManager()
    seen: Set[str] = set()
    for d in [os.path.dirname(p) for p in paths] + list(include_dirs):
        d = os.path.realpath(d) if d else os.path.realpath(".")
        if d and d not in seen:
            seen.add(d)
            sm.addUserDirectories(d)

    opts = pyslang.Bag()
    pp = PreprocessorOptions()
    if defines:
        pp.predefines = list(defines)
    opts.preprocessorOptions = pp
    return sm, opts


def _parse_all(paths: Sequence[str], sm: Any, opts: Any) -> Any:
    for path in paths:
        if not os.path.isfile(path):
            raise FileNotFoundError(2, "No such file or directory", path)

    tree = SyntaxTree.fromFiles(list(paths), sm, opts)

    engine = pyslang.DiagnosticEngine(sm)
    fatal = [
        d for d in tree.diagnostics
        if engine.getSeverity(d.code, d.location) in (
            pyslang.DiagnosticSeverity.Error,
            pyslang.DiagnosticSeverity.Fatal,
        )
    ]
    if fatal:
        # One compilation unit means one failure report; name the file the first
        # diagnostic points at so the exception sidentifies a real source.
        blamed = _file_of(sm, fatal[0].location) or (paths[0] if paths else "<none>")
        raise RtlParseError(blamed, pyslang.DiagnosticEngine.reportAll(sm, fatal))
    return tree


def _file_of(sm: Any, loc: Any) -> Optional[str]:
    """Absolute path of the file a source location belongs to, if resolvable."""
    try:
        name = sm.getFileName(loc)
    except Exception:  # pragma: no cover - defensive; slang may lack the buffer
        return None
    return os.path.realpath(name) if name else None


# ── Canonical serialization ─────────────────────────────────────────────────

def _token_text(tok: Any) -> str:
    """Canonical text for one token, normalizing spelling-only differences."""
    kind = tok.kind.name
    if kind == "Identifier":
        # \escaped_id and escaped_id are distinct identifiers; valueText strips
        # the backslash, so put a marker back to keep them apart.
        return ("\\" + tok.valueText) if tok.rawText.startswith("\\") else tok.valueText
    if kind == "IntegerBase":
        return tok.valueText.lower()          # 'H == 'h
    if kind in _NUMERIC_TOKENS:
        return tok.valueText.replace("_", "").lower()   # 16'hA_b == 16'hab
    return tok.valueText


def _emit_directives(parts: List[str], tok: Any) -> None:
    """Emit the semantically meaningful preprocessor directives before a token."""
    for tr in tok.trivia:
        if tr.kind != TriviaKind.Directive:
            continue
        node = tr.syntax()
        if node is None or node.kind.name not in _SEMANTIC_DIRECTIVES:
            continue
        _canon_into(parts, node)

_CLOSE = ")"
_CLOSE_AST = object()   # sentinel closing an AST scope in _canon_ast

def _canon_into(parts: List[str], node: Any) -> int:
    """
    Append the canonical serialization of ``node`` to ``parts``.

    Nodes become ``(<SyntaxKind> ... )``, absent optional children become ``~``,
    and tokens become ``<TokenKind>:<text>``.  Trivia -- comments, whitespace,
    newlines -- is never visited and makes the fingerprint formatting-insensitive.  
    Returns the number of tokens emitted.
    """
    ntok = 0
    stack: List[Any] = [node]
    while stack:
        item = stack.pop()
        if item is _CLOSE:
            parts.append(")")
            continue
        if item is None:
            parts.append("~")
            continue
        if isinstance(item, Token):
            if item.kind.name == "EndOfFile":
                continue
            _emit_directives(parts, item)
            parts.append(item.kind.name + ":" + _token_text(item))
            ntok += 1
            continue
        parts.append("(" + item.kind.name)
        stack.append(_CLOSE)
        for i in range(len(item) - 1, -1, -1):
            stack.append(item[i])
    return ntok


# ── Elaboration: the semantic (AST) model ───────────────────────────────────

def _elaborate(tree: Any, top_module: Optional[str] = None) -> Any:
    """
    `Tool reads ``synthesis.inputs.input_files``
    *plus* the technology's verilog_synth wrappers, and resolves true leaf cells
    (standard cells, SRAM macros) from liberty rather than RTL -- an unresolved
    instance becomes a black box and elaboration continues.  The fingerprint sees
    only ``input_files``, so it is working with a deliberately incomplete design:
    without this flag no design instantiating a macro could be fingerprinted.
    """
    opts = ast.CompilationOptions()
    opts.flags = opts.flags | ast.CompilationFlags.IgnoreUnknownModules
    if top_module:
        opts.topModules = {top_module}
    bag = pyslang.Bag()
    bag.compilationOptions = opts
    comp = ast.Compilation(bag)
    comp.addSyntaxTree(tree)
    return comp


def _is_transparent_block(node: Any) -> bool:
    """
    True for a ``begin``/``end`` that carries no meaning.

    Only an *unnamed*, *sequential* block wrapping a *single* statement is pure
    punctuation -- writing ``if (x) y <= 1;`` and ``begin if (x) begin y <= 1;
    end end`` must fingerprint the same.  Everything else stays:

    * a named block (``begin : lbl``) can be referenced hierarchically and can
      hold declarations, so its ``blockSymbol`` is not None;
    * ``fork``/``join`` changes execution semantics, so ``blockKind`` is not
      Sequential;
    * a block over several statements has a ``StatementList`` body, and dropping
      it would splice those statements into the parent, which can collide with a
      genuinely different nesting.
    """
    if type(node).__name__ != "BlockStatement":
        return False
    if getattr(node, "blockSymbol", None) is not None:
        return False
    if str(getattr(node, "blockKind", "")) != "StatementBlockKind.Sequential":
        return False
    return type(getattr(node, "body", None)).__name__ != "StatementList"


def _ast_descriptor(node: Any) -> Optional[str]:
    """
    Canonical text for one AST node, or None if the node carries no meaning.

    Elaboration has already folded constants and resolved types, so ``[8-1:0]``
    and ``[7:0]`` arrive as the same type and ``8'd3`` and ``8'h3`` as the same
    value -- none of that has to be normalized by hand the way the token text
    did.  What is emitted is the node kind plus the fields that distinguish two
    nodes of that kind: the operator, the symbol referenced, the constant value,
    the resolved type, and whatever ``_DISCRIMINATORS`` lists for that kind.
    """
    if _is_transparent_block(node):
        return None

    parts: List[str] = [type(node).__name__]
    for attr in ("op", "direction", "blockKind"):
        val = getattr(node, attr, None)
        if val is not None:
            parts.append(str(val))
    for attr in _DISCRIMINATORS.get(type(node).__name__, ()):
        val = getattr(node, attr, None)
        # `is not None` and not a truth test: isNonBlocking is a bool, and False
        # must not collapse into "attribute absent".
        if val is not None:
            parts.append(f"{attr}={val}")
    name = getattr(node, "name", None)
    if name:
        parts.append(f"name={name}")
    symbol = getattr(node, "symbol", None)
    if symbol is not None and getattr(symbol, "name", None):
        parts.append(f"ref={symbol.name}")
    if type(node).__name__ in ("IntegerLiteral", "ParameterSymbol"):
        value = getattr(node, "value", None)
        if value is not None:
            parts.append(f"val={value}")
    typ = getattr(node, "type", None)
    if typ is not None:
        # The canonical type, never the alias name: `str(type)` renders a typedef
        # as "nib_t", so widening `typedef logic [3:0] nib_t` to [7:0] would be
        # invisible in every module that uses it.  canonicalType gives
        # "logic[3:0]" and makes the change land.
        parts.append(f"type={getattr(typ, 'canonicalType', None) or typ}")
    return " ".join(parts)


def _body_identity(body: Any) -> str:
    """
    Stable identity for one elaborated module body.

    NOT ``id()``.  pyslang hands back a fresh Python wrapper on every ``.body``
    access; the temporary is freed immediately and the next access reuses the
    address, so ``id(a.body) == id(b.body)`` comes out True for two completely
    different modules.  Deduplicating on that silently drops units -- six of the
    fourteen files in a real design vanished from the fingerprint this way.

    The name plus the elaborated parameter values is stable and is exactly the
    granularity wanted: ``mux #(8)`` and ``mux #(16)`` are different bodies and
    get separate units, while repeated instances of the same specialization
    collapse into one.
    """
    params = []
    for member in body:
        # Real parameters only.  localparams are derived from them, so including
        # them adds no distinguishing power and makes the key unreadable -- one
        # real cache module carries 18 of them.
        if (type(member).__name__ == "ParameterSymbol"
                and not getattr(member, "isLocalParam", False)):
            params.append(f"{member.name}={getattr(member, 'value', '')}")
    return body.name + ("#(" + ",".join(params) + ")" if params else "")


def _canon_ast(body: Any, found: List[Any]) -> Tuple[str, int]:
    """
    Canonical string + node count for one elaborated design unit.

    Stops at instance boundaries.  ``visit`` on its own descends into child
    instance bodies, which would fold the whole subtree into every ancestor --
    ``riscv_top`` then hashes all 10801 nodes of the design instead of its own
    600, and one leaf edit rewrites every hash up the hierarchy.  A child is
    emitted as a reference here and hashed as its own unit; instances met along
    the way are appended to ``found`` so the caller can queue them.

    Scopes are walked member by member so nested instances (inside a generate
    block, say) are still discovered.  Everything else is handed to ``visit``,
    which cannot contain an instance and whose pre-order stream is unambiguous
    for the fixed-arity nodes making up expressions -- Polish notation, so
    ``(p&q)|r`` and ``p&(q|r)`` serialize differently.
    """
    parts: List[str] = []

    def emit(node: Any) -> None:
        text = _ast_descriptor(node)
        if text is not None:
            parts.append(text)

    stack: List[Any] = [_CLOSE_AST if m is _CLOSE_AST else m
                        for m in reversed(list(body))]
    while stack:
        sym = stack.pop()
        if sym is _CLOSE_AST:
            parts.append(")")
            continue
        if type(sym).__name__ == "InstanceSymbol":
            # Boundary: name the child so a swapped submodule still shows up,
            # but leave its contents to its own unit.
            child = getattr(sym, "canonicalBody", None) or sym.body
            parts.append(f"Instance ref={child.name} inst={sym.name}")
            found.append(sym)
            continue
        if type(sym).__name__ == "UninstantiatedDefSymbol":
            # A tech macro or SRAM.  It has no definition in input_files -- Genus
            # resolves those from liberty -- so IgnoreUnknownModules leaves it
            # unelaborated, and `_ast_descriptor` would match only `name`, which
            # here is the *instance* name.  Two different macros then serialize
            # identically and a swapped SRAM reads as "RTL unchanged".
            #
            # Nothing evaluates a blackbox's parameters either (paramExpressions
            # come back Invalid), so hash the instantiation syntax instead: that
            # covers the definition name, the parameters and the port
            # connections in one pass.  The cost is that an unfolded constant --
            # `#(.W(4+4))` vs `#(.W(8))` -- looks like a change on this one
            # instantiation, which over-triggers a re-run rather than missing one.
            parts.append(f"Blackbox def={sym.definitionName} inst={sym.name}")
            syntax = getattr(sym, "syntax", None)
            if syntax is not None:
                # `syntax` is the per-instance HierarchicalInstance, so it holds
                # the instance name and the port connections but NOT the
                # parameter list -- that hangs off the parent statement, shared
                # by every instance declared in it.  Emit it first so
                # `foo #(.W(8)) u()` and `foo #(.W(16)) u()` differ.
                stmt = getattr(syntax, "parent", None)
                params = getattr(stmt, "parameters", None) if stmt is not None else None
                if params is not None:
                    _canon_into(parts, params)
                _canon_into(parts, syntax)
            continue
        if getattr(sym, "isScope", False):
            text = _ast_descriptor(sym)
            parts.append("(" + (text or type(sym).__name__))
            stack.append(_CLOSE_AST)
            stack.extend(reversed(list(sym)))
            continue
        sym.visit(emit)
    return " ".join(parts), len(parts)


def _raise_on_errors(comp: Any, sm: Any, fallback: str) -> None:
    """Elaboration errors are design errors; surface them like parse errors."""
    engine = pyslang.DiagnosticEngine(sm)
    fatal = []
    for d in comp.getAllDiagnostics():
        severity = engine.getSeverity(d.code, d.location)
        if severity in (pyslang.DiagnosticSeverity.Error,
                        pyslang.DiagnosticSeverity.Fatal):
            fatal.append(d)

    if fatal:
        blamed = _file_of(sm, fatal[0].location) or fallback
        raise RtlParseError(blamed,
                            pyslang.DiagnosticEngine.reportAll(sm, fatal))


def _warn_uncovered(paths: Sequence[str], buckets: Dict[str, Any]) -> None:
    """
    Warn about listed files that contributed nothing to the fingerprint.

    Elaborating from a top module is what makes a dead module stop forcing a
    re-run, which is the point.  The same mechanism means a stale or misspelled
    ``top_module`` silently shrinks coverage while still returning a
    healthy-looking hash -- so say so.  Diagnostic only: this never raises and
    never touches a hash.
    """
    covered = {entry[1] for entries in buckets.values() for entry in entries}
    missing = [p for p in paths if p not in covered]
    if missing and len(missing) != len(paths):
        print(f"rtl_check: {len(missing)} of {len(paths)} input files contributed no "
              f"design units; check synthesis.inputs.top_module reaches them:",
              file=sys.stderr)
        for p in missing:
            print(f"  {p}", file=sys.stderr)


def _collect_directives(tree: Any) -> Optional[str]:
    """
    Hash of the semantic preprocessor directives across the whole design.

    ``\\`timescale`` and friends never reach the AST -- the preprocessor consumes
    them and they survive only as trivia on the syntax tree -- so they have to be
    picked up separately or a timescale change would be invisible.
    """
    parts: List[str] = []
    root = tree.root
    stack: List[Any] = [root[i] for i in range(len(root) - 1, -1, -1)]
    while stack:
        item = stack.pop()
        if item is None:
            continue
        if isinstance(item, Token):
            _emit_directives(parts, item)
            continue
        for i in range(len(item) - 1, -1, -1):
            stack.append(item[i])
    return " ".join(parts) if parts else None

# ── Digesting ───────────────────────────────────────────────────────────────

def digest_units(paths: Sequence[str],
                 include_dirs: Sequence[str] = (),
                 defines: Sequence[str] = (),
                 top_module: Optional[str] = None) -> Tuple[str, List[DesignUnit]]:
    """
    Parse every input file and hash each top-level design unit separately.

    All inputs form ONE compilation unit, in the order given, so macros cross
    file boundaries exactly as they do under VCS or Genus.  That makes the order
    of ``paths`` significant: a macro has to be defined by an earlier file to be
    visible in a later one.  Reordering ``synthesis.inputs.input_files`` can
    therefore move the fingerprint deliberately, since it can also change what
    the tools compile.

    :param paths: RTL source files (.v/.sv), in the order the tools see them.
        Header fragments (.vh/.svh) must NOT be listed here: a listed header is
        parsed as a source in its own right rather than inlined at its `include
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

    sm, opts = _build_context(real_paths, include_dirs, defines)

    # key -> list of (unit hash, source file, node count)
    buckets: Dict[str, List[Tuple[str, str, int]]] = {}

    def add(key: str, unit_sha: str, path: str, ntok: int) -> None:
        buckets.setdefault(key, []).append((unit_sha, path, ntok))

    tree = _parse_all(real_paths, sm, opts)
    fallback = real_paths[0] if real_paths else "<none>"

    # `comp` owns every symbol walked below.  It MUST stay referenced for the
    # whole walk: letting it be collected mid-traversal silently truncates the
    # AST -- expressions vanish entirely, and distinct designs then hash alike.
    comp = _elaborate(tree, top_module)
    _raise_on_errors(comp, sm, fallback)

    # One unit per distinct elaborated body.  slang shares canonicalBody between
    # instances of the same specialization, so this dedups repeated instances
    # while keeping mux #(8) and mux #(16) apart.
    seen_bodies: Set[str] = set()
    stack: List[Any] = list(comp.getRoot().topInstances)
    while stack:
        inst = stack.pop()
        body = getattr(inst, "canonicalBody", None) or inst.body
        marker = _body_identity(body)
        if marker in seen_bodies:
            continue
        seen_bodies.add(marker)

        children: List[Any] = []
        text, ntok = _canon_ast(body, children)
        sha = sha256_hex(text.encode("utf-8"))
        owner = _file_of(sm, body.location) or fallback
        # Key on the plain name, not the specialization: Verilog-2001 declares
        # ordinary parameters in the module body, so a real module can carry a
        # dozen and the key becomes unreadable.  Two specializations bucket under
        # one key and their hashes combine, which still distinguishes them.
        add(f"module:{body.name}" if body.name else f"$unit:{sha[:16]}",
            sha, owner, ntok)
        stack.extend(children)

    # `timescale and friends are consumed by the preprocessor and never reach
    # the AST, so they are picked up from the syntax tree separately.
    directives = _collect_directives(tree)
    if directives:
        sha = sha256_hex(directives.encode("utf-8"))
        add(f"$directives:{sha[:16]}", sha, fallback, 0)

    _warn_uncovered(real_paths, buckets)

    units: List[DesignUnit] = []
    for key in sorted(buckets):
        entries = sorted(buckets[key])
        if len(entries) == 1:
            combined = entries[0][0]
        else:
            # Same key defined more than once (duplicate module definitions, or
            # the same header pulled into several files).  Hash the sorted set
            # of member hashes so this stays deterministic instead of erroring.
            combined = sha256_hex("\n".join(e[0] for e in entries).encode("utf-8"))
        files = ",".join(sorted({e[1] for e in entries}))
        units.append(DesignUnit(key=key, sha256=combined, files=files,
                                tokens=sum(e[2] for e in entries)))

    manifest = "\n".join(f"{u.key} {u.sha256}" for u in units)
    return sha256_hex(manifest.encode("utf-8")), units


def digest_files(paths: Sequence[str],
                 include_dirs: Sequence[str] = (),
                 defines: Sequence[str] = (),
                 top_module: Optional[str] = None) -> Tuple[str, List[DesignUnit]]:
    """Backwards-compatible alias for :func:`digest_units`."""
    return digest_units(paths, include_dirs=include_dirs, defines=defines,
                        top_module=top_module)


# ── Output ──────────────────────────────────────────────────────────────────

def format_fingerprint(overall: str, units: Sequence[DesignUnit]) -> str:
    lines = [f"overall_sha256 {overall}\n"]
    for u in units:
        lines.append(f"{u.sha256}  {u.key}  {u.files}  tokens={u.tokens}\n")
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
        description="Compute an RTL fingerprint from the SystemVerilog syntax tree.")
    parser.add_argument("--out", required=True, help="Output fingerprint file path.")
    parser.add_argument("-I", "--include-dir", action="append", default=[],
                        metavar="DIR", help="Extra `include search directory (repeatable).")
    parser.add_argument("-D", "--define", action="append", default=[],
                        metavar="NAME[=VALUE]", help="Predefined macro (repeatable).")
    parser.add_argument("inputs", nargs="+", help="Input RTL files (.v/.sv).")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        overall, units = digest_units(args.inputs,
                                      include_dirs=args.include_dir,
                                      defines=args.define)
    except RtlParseError as e:
        print(e.report, file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"RTL file not found: {e.filename}", file=sys.stderr)
        return 1

    write_if_changed(args.out, format_fingerprint(overall, units))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
