#!/usr/bin/env python3
"""Short path to the helper from a clone: forwards to skills/find-session/scripts/sessions.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find-session" / "scripts"))

from find_session.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
