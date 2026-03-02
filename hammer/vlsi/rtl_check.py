#  Generate a stable RTL fingerprint that ignores formatting.

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple


_WS = set(b" \t\r\n\v\f")


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
    - collapsing whitespace to a single ASCII space
    - preserving string literals
    """
    out = bytearray()
    pending_space = False
    i = 0
    n = len(data)

    def emit_byte(b: int) -> None:
        nonlocal pending_space
        if pending_space and len(out) > 0 and out[-1] != 0x20:
            out.append(0x20)  # space
        pending_space = False
        out.append(b)

    while i < n:
        c = data[i]

        # Whitespace collapsing (except strings/comments)
        if c in _WS:
            pending_space = True
            i += 1
            continue

        # Line comment //
        if c == 0x2F and i + 1 < n and data[i + 1] == 0x2F:
            # Skip until newline (or end)
            i += 2
            while i < n and data[i] not in (0x0A, 0x0D):  # \n or \r
                i += 1
            # Consume one newline char if present (normalizes CRLF/CR)
            if i < n and data[i] in (0x0A, 0x0D):
                i += 1
                # If CRLF, consume the LF too
                if i < n and data[i - 1] == 0x0D and data[i] == 0x0A:
                    i += 1
            pending_space = True
            continue

        # Block comment /* ... */
        if c == 0x2F and i + 1 < n and data[i + 1] == 0x2A:
            i += 2
            while i + 1 < n and not (data[i] == 0x2A and data[i + 1] == 0x2F):
                i += 1
            if i + 1 < n:
                i += 2  # consume */
            else:
                i = n
            pending_space = True
            continue

        # String literal " ... "
        if c == 0x22:
            if pending_space and len(out) > 0 and out[-1] != 0x20:
                out.append(0x20)
            pending_space = False
            out.append(0x22)
            i += 1
            while i < n:
                ch = data[i]
                out.append(ch)
                if ch == 0x5C:  # backslash
                    i += 1
                    if i < n:
                        out.append(data[i])  # escaped char
                elif ch == 0x22:  # closing quote
                    break
                i += 1
            i += 1
            continue

        # Default: passthrough byte, inserting pending single space if needed.
        emit_byte(c)
        i += 1

    # Trim trailing space if any
    while len(out) > 0 and out[-1] == 0x20:
        out.pop()

    return bytes(out)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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