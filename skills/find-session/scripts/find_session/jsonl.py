"""Tolerant reading of JSONL session files."""

from __future__ import annotations

import json
from pathlib import Path
from zlib import crc32

if False:  # typing only
    from typing import Iterable

# Bounded reads: one oversized record (pasted image, huge tool output) is
# skipped instead of being materialized, and one file never costs more than
# MAX_FILE_BYTES of I/O.
MAX_LINE_BYTES = 16 * 1024 * 1024

MAX_FILE_BYTES = 512 * 1024 * 1024


class Progress:
    """How far a file has been read, for continuing later instead of starting over.

    `offset` is the end of the last complete line that was handled and `crc`
    the CRC-32 of the file up to there. `resumable` turns false when something
    past `offset` was handed out (a last line without its newline that parsed
    anyway): the file cannot be continued from `offset` then.
    """

    __slots__ = ("offset", "crc", "resumable")

    def __init__(self, offset: int = 0, crc: int = 0):
        self.offset = offset
        self.crc = crc
        self.resumable = True


def iter_json_lines(path: Path, progress: Progress | None = None, *, skip: bytes = b"") -> Iterable[dict]:
    """Yield JSON objects from a JSONL (or .jsonl.gz) file, skipping bad lines.

    Partial trailing writes, malformed lines, non-object rows and records over
    MAX_LINE_BYTES are skipped; the file is never modified. With `progress`,
    reading starts at `progress.offset` and the object is kept up to date.
    A line that begins with `skip` is passed over without being decoded: for
    records the caller would drop anyway, when they are large and many.
    """
    if path.name.endswith(".gz"):
        import gzip

        opener = gzip.open
    else:
        opener = open
    consumed = progress.offset if progress else 0
    crc = progress.crc if progress else 0
    with opener(path, "rb") as handle:
        if consumed:
            handle.seek(consumed)
        while consumed < MAX_FILE_BYTES:
            try:
                line = handle.readline(MAX_LINE_BYTES + 1)
            except (OSError, EOFError):
                return
            if not line:
                return
            consumed += len(line)
            if progress:
                crc = crc32(line, crc)
            if len(line) > MAX_LINE_BYTES:
                while line and not line.endswith(b"\n") and consumed < MAX_FILE_BYTES:
                    try:
                        line = handle.readline(MAX_LINE_BYTES + 1)
                    except (OSError, EOFError):
                        return
                    consumed += len(line)
                    if progress:
                        crc = crc32(line, crc)
                if progress and line.endswith(b"\n"):
                    progress.offset, progress.crc = consumed, crc
                continue
            complete = line.endswith(b"\n")
            if progress and complete:
                # Set before the object is handed out: whoever continues from
                # here has processed everything up to this line.
                progress.offset, progress.crc = consumed, crc
            if skip and line.startswith(skip):
                continue
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                if progress and not complete:
                    progress.resumable = False
                yield obj


def prefix_crc(path: Path, length: int) -> int | None:
    """CRC-32 of the first `length` bytes of a file; None when it is shorter or unreadable."""
    crc = 0
    left = length
    try:
        with open(path, "rb") as handle:
            while left:
                block = handle.read(min(left, 1 << 20))
                if not block:
                    return None
                crc = crc32(block, crc)
                left -= len(block)
    except OSError:
        return None
    return crc
