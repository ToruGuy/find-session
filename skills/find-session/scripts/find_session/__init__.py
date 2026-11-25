"""find-session: search past Claude Code, Codex, omp (oh-my-pi) and Antigravity CLI (agy) conversations.

Only conversation text and the commands of tool calls are extracted. Tool output,
reasoning, injected context and anything that looks like a secret are never
indexed, and session files are only ever read.
"""

import sys

# Checked here, before any other module of the package is read, and written so
# that an older Python can still run it: every way in (the entry script, the
# command, `python -m`) passes this line first.
if sys.version_info < (3, 9):  # noqa: UP036
    sys.stderr.write(
        "find-session: Python 3.9 or newer is needed, but %s is Python %d.%d.\n"  # noqa: UP031
        % (sys.executable or "this", sys.version_info[0], sys.version_info[1])
    )
    # The same exit code the npx wrapper uses when it finds no usable Python.
    raise SystemExit(3)

from .config import version  # noqa: E402

__all__ = ["version"]
