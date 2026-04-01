#  Generate a stable RTL fingerprint that ignores formatting.

from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Set, Tuple


_INLINE_WS = set(b" \t\v\f")   # whitespace that does not end a line
_TAB_WIDTH = 4


@dataclass(frozen=True)
class FileDigest:
    path: str
    sha256: str
    normalized_len: int

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute RTL fingerprint ignoring comments/whitespace.")
    parser.add_argument("--out", required=True, help="Output fingerprint file path.")
    parser.add_argument("inputs", nargs="+", help="Input RTL files (.v/.sv).")
    args = parser.parse_args(list(argv) if argv is not None else None)

    overall, digests = digest_files(args.inputs)
    contents = format_fingerprint(overall, digests)
    write_if_changed(args.out, contents)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

def normalize_verilog_bytes(data: bytes) -> bytes:
    """
    Normalize Verilog/SystemVerilog-ish source by:
    - stripping // and /* */ comments
    - stripping blank lines
    - normalizing leading indentation (tabs expanded to _TAB_WIDTH spaces, then preserved)
    - collapsing inline whitespace runs to a single ASCII space
    - preserving string literals
    Indentation changes are detectable; tab-vs-space reformatting at the same
    indent level is not.
    """
    out = bytearray()
    i = 0
    n = len(data)
    at_line_start = True   # True at file start and after every newline
    indent = bytearray()   # normalized leading whitespace for the current line
    pending_space = False  # need a space before next inline token

    def skip_newline(pos: int) -> int:
        """Consume one LF, CR, or CRLF at pos."""
        if pos < n:
            if data[pos] == 0x0D:
                pos += 1
                if pos < n and data[pos] == 0x0A:
                    pos += 1
            elif data[pos] == 0x0A:
                pos += 1
        return pos

    def begin_line_content() -> None:
        """Emit newline separator + accumulated indent before first token on a line."""
        nonlocal pending_space
        if out:                 # not the very first content line
            out.append(0x0A)
        out.extend(indent)
        pending_space = False

    def emit_byte(b: int) -> None:
        nonlocal pending_space
        if pending_space:
            last = out[-1] if out else None
            if last is not None and last not in (0x20, 0x0A):
                out.append(0x20)
        pending_space = False
        out.append(b)

    while i < n:
        c = data[i]

        # ── At start of line: accumulate normalized indent ──────────────────
        if at_line_start:
            if c == 0x20:                                   # space
                indent.append(0x20)
                i += 1
                continue
            if c == 0x09:                                   # tab → expand
                indent.extend(b' ' * _TAB_WIDTH)
                i += 1
                continue
            if c in (0x0A, 0x0D):                          # blank line
                indent.clear()
                i = skip_newline(i)
                continue
            if c == 0x2F and i + 1 < n and data[i + 1] == 0x2F:   # // whole-line comment
                i += 2
                while i < n and data[i] not in (0x0A, 0x0D):
                    i += 1
                i = skip_newline(i)
                indent.clear()
                continue
            if c == 0x2F and i + 1 < n and data[i + 1] == 0x2A:   # /* */ at line start
                i += 2
                has_newline = False
                while i + 1 < n and not (data[i] == 0x2A and data[i + 1] == 0x2F):
                    if data[i] in (0x0A, 0x0D):
                        has_newline = True
                    i += 1
                if i + 1 < n:
                    i += 2
                else:
                    i = n
                if has_newline:
                    indent.clear()  # at_line_start remains True
                continue           # keep at_line_start=True; real content follows
            # First real token on this line
            begin_line_content()
            indent.clear()
            at_line_start = False
            # fall through to mid-line processing for c

        # ── Mid-line processing ─────────────────────────────────────────────
        if c in (0x0A, 0x0D):
            at_line_start = True
            indent.clear()
            pending_space = False
            i = skip_newline(i)
            continue

        if c in _INLINE_WS:
            pending_space = True
            i += 1
            continue

        if c == 0x2F and i + 1 < n and data[i + 1] == 0x2F:   # // inline comment
            i += 2
            while i < n and data[i] not in (0x0A, 0x0D):
                i += 1
            i = skip_newline(i)
            at_line_start = True
            indent.clear()
            pending_space = False
            continue

        if c == 0x2F and i + 1 < n and data[i + 1] == 0x2A:   # /* */ inline
            i += 2
            has_newline = False
            while i + 1 < n and not (data[i] == 0x2A and data[i + 1] == 0x2F):
                if data[i] in (0x0A, 0x0D):
                    has_newline = True
                i += 1
            if i + 1 < n:
                i += 2
            else:
                i = n
            if has_newline:
                at_line_start = True
                indent.clear()
                pending_space = False
            else:
                pending_space = True
            continue

        if c == 0x22:                                          # string literal
            if pending_space:
                last = out[-1] if out else None
                if last is not None and last not in (0x20, 0x0A):
                    out.append(0x20)
            pending_space = False
            out.append(0x22)
            i += 1
            while i < n:
                ch = data[i]
                out.append(ch)
                if ch == 0x5C:      # backslash escape
                    i += 1
                    if i < n:
                        out.append(data[i])
                elif ch == 0x22:    # closing quote
                    break
                i += 1
            i += 1
            continue

        emit_byte(c)
        i += 1

    # Trim trailing whitespace
    while out and out[-1] in (0x20, 0x0A):
        out.pop()

    return bytes(out)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_INCLUDE_RE = re.compile(r'(?m)^(?!\s*//).*`include\s+"([^"]+\.vh)"')


def collect_include_files(src_paths: Sequence[str]) -> List[str]:
    """
    Scan .v/.sv source files for `include "*.vh" directives and return the
    resolved, deduplicated list of .vh paths.  Resolves relative to the
    including file's directory and recurses into discovered .vh files.
    Skips `include lines beginning with // lines.
    """
    found: Set[str] = {os.path.realpath(p) for p in src_paths}
    queue: List[str] = list(found)
    while queue:
        path = queue.pop()
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        base_dir = os.path.dirname(path)
        for match in _INCLUDE_RE.finditer(data):
            inc_name = match.group(1)
            inc_path = os.path.realpath(os.path.join(base_dir, inc_name))
            if inc_path not in found:
                found.add(inc_path)
                queue.append(inc_path)
    return sorted(found - {os.path.realpath(p) for p in src_paths})


def digest_file(path: str) -> FileDigest:
    with open(path, "rb") as f:
        raw = f.read()
    norm = normalize_verilog_bytes(raw)
    return FileDigest(path=os.path.realpath(path), sha256=sha256_hex(norm), normalized_len=len(norm))


def digest_files(paths: Sequence[str]) -> Tuple[str, List[FileDigest]]:
    realpaths = sorted({os.path.realpath(p) for p in paths})
    digests = [digest_file(p) for p in realpaths]
    manifest_lines = [f"{d.sha256}  {d.path}\n" for d in digests]
    overall = sha256_hex("".join(manifest_lines).encode("utf-8"))
    return overall, digests


def format_fingerprint(overall: str, digests: Sequence[FileDigest]) -> str:
    lines = [f"overall_sha256 {overall}\n"]
    for d in digests:
        lines.append(f"{d.sha256}  {d.path}  normalized_len={d.normalized_len}\n")
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