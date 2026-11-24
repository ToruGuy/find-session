"""Version, optional config file and the history home directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[4] / "VERSION"


def version() -> str:
    # The repository's VERSION in a clone; the copy shipped inside the skill folder otherwise.
    for candidate in (VERSION_FILE, Path(__file__).resolve().parents[2] / "VERSION"):
        try:
            return candidate.read_text().strip() or "unknown"
        except OSError:
            continue
    # Installed as a package (pip, pipx, uv tool): no VERSION file next to the code.
    # Found by its console script, not by name: the distribution name on PyPI can
    # differ from "find-session", and an unrelated "find-session" may be installed.
    try:
        from importlib.metadata import distributions

        for dist in distributions():
            if any(entry.value.startswith("find_session.") for entry in dist.entry_points):
                return dist.version
    except Exception:
        pass
    return "unknown"


def config_path() -> Path:
    configured = os.environ.get("FIND_SESSION_CONFIG")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "find-session/config.json"


def load_config() -> dict:
    """Optional per-machine settings; a missing or broken file means defaults.

    Keys (all optional):
      home          directory holding .claude/.codex/.omp/.gemini (e.g. a synced mirror)
      resume_label  label of the resume hint line (default "Resume")
      hints         extra lines printed under each search result:
                    [{"label": "...", "command": "... {selector} ..."}]
                    placeholders: {selector} {id} {source} {cwd} {resume}
      recent_include  globs (relative to home) of files appended to `recent`
    """
    path = config_path()
    try:
        text = path.read_text()
        # Imported here: most machines have no config file, and every run starts here.
        import json

        data = json.loads(text)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        print(f"find-session: ignoring config {path}: {error}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def default_home(config: dict | None = None) -> Path:
    """Base directory holding .claude, .codex, .omp and .gemini: env, config, then ~."""
    configured = os.environ.get("FIND_SESSION_HOME")
    if configured:
        return Path(configured).expanduser()
    config = load_config() if config is None else config
    home = config.get("home")
    if isinstance(home, str) and home:
        return Path(home).expanduser()
    return Path.home()
