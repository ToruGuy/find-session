#!/usr/bin/env python3
"""Entry point of find-session: `python3 sessions.py search|inspect|recent|index|sources`.

The code lives in the `find_session` package next to this file.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from find_session.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
