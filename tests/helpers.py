"""Synthetic fixtures and runners shared by the tests. Nothing here is real session data."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find-session" / "scripts"))

from find_session import (
    cli,
)

# Real overrides and config on the developer machine must never leak into fixtures,
# and tests parse fixtures directly: the cache has its own tests.
for _name in (
    "FIND_SESSION_HOME",
    "FIND_SESSION_CONFIG",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "CLAUDE_SESSIONS_ROOT",
    "CODEX_SESSIONS_ROOT",
    "OMP_SESSIONS_ROOT",
    "PI_CODING_AGENT_DIR",
    "PI_CONFIG_DIR",
    # The tests may be run by an agent: its own session must not count as the caller.
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
):
    os.environ.pop(_name, None)
os.environ["FIND_SESSION_CONFIG"] = str(Path(tempfile.gettempdir()) / "find-session-tests-no-config.json")
os.environ["FIND_SESSION_CACHE"] = "off"
# A slow machine must not add progress lines to output that tests compare; test_cli covers them.
os.environ["FIND_SESSION_PROGRESS"] = "off"
for _name in ("FIND_SESSION_PROG", "FIND_SESSION_COMMAND"):
    os.environ.pop(_name, None)


def run_raw(*argv: str) -> tuple[int, str]:
    """Run the CLI in-process; returns (exit code, stdout + stderr)."""
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        code = cli.main(list(argv))
    return code, captured.getvalue()


def run_full(*argv: str) -> tuple[int, str]:
    """Like run_raw, but searches print the full, labelled format (--full).

    Most tests check what is found and shown; test_output covers the compact default.
    """
    argv = list(argv)
    if "search" in argv and "--json" not in argv:
        argv.insert(argv.index("search") + 1, "--full")
    return run_raw(*argv)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def claude_title(kind: str, title: str, session_id: str = "synthetic") -> dict:
    """A Claude Code title record: kind "custom" (the user's name for the session) or "ai" (generated)."""
    return {"type": f"{kind}-title", f"{kind}Title": title, "sessionId": session_id}


def write_codex_titles(home: Path, *rows: tuple[str, str]) -> Path:
    """Codex's thread names file: one (session id, name) row per rename, in order."""
    path = home / ".codex/session_index.jsonl"
    write_jsonl(
        path,
        [{"id": session_id, "thread_name": name, "updated_at": "2026-09-22T10:00:00Z"} for session_id, name in rows],
    )
    return path


OMP_BUCKET = ".omp/agent/sessions/-projects-demo"


MAIN_STEM = "2026-09-22T10-00-00-000Z_omp-main-1"


def omp_title_slot(title: str) -> str:
    slot = json.dumps({"type": "title", "title": title, "source": "auto"})
    return slot.ljust(255) + "\n"


def omp_header(session_id: str, cwd: str = "/Users/alex/projects/demo") -> dict:
    return {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-09-22T10:00:00.000Z",
        "cwd": cwd,
    }


def omp_msg(entry_id: str, parent: str | None, role: str, content) -> dict:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent,
        "timestamp": "2026-09-22T10:00:01.000Z",
        "message": {"role": role, "content": content},
    }


def write_omp(path: Path, rows: list, *, title: str | None = "Synthetic", tail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = omp_title_slot(title) if title else ""
    for row in rows:
        body += row if isinstance(row, str) else json.dumps(row) + "\n"
    path.write_text(body + tail)


def main_omp_rows() -> list:
    """Synthetic main session: tools, reasoning, injected entries and a rewind."""
    return [
        omp_header("omp-main-1"),
        {"type": "model_change", "id": "m0", "parentId": None, "model": "openai-codex/x"},
        omp_msg("u1", "m0", "user", "Plan the pilot PILOTTOPIC"),
        omp_msg(
            "a1",
            "u1",
            "assistant",
            [
                {"type": "thinking", "thinking": "SYNTH_REASONING"},
                {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "SYNTH_TOOL_ARGS"}},
                {"type": "text", "text": "Plan ready"},
            ],
        ),
        omp_msg("r1", "a1", "toolResult", [{"type": "text", "text": "SYNTH_TOOL_OUTPUT"}]),
        omp_msg("d1", "r1", "developer", "SYNTH_DEVELOPER"),
        {
            "type": "custom_message",
            "id": "c1",
            "parentId": "d1",
            "customType": "x",
            "content": "SYNTH_INJECTED",
            "display": True,
        },
        omp_msg("b1", "c1", "bashExecution", "SYNTH_BASH"),
        "this line is not json\n",
        omp_msg("u2", "b1", "user", "Old branch ABANDONED"),
        omp_msg("a2", "u2", "assistant", [{"type": "text", "text": "ABANDONED_ANSWER"}]),
        {"type": "branch_summary", "id": "s1", "parentId": "b1", "fromId": "b1", "summary": "SYNTH_SUMMARY"},
        omp_msg("u3", "s1", "user", "New branch"),
        omp_msg("a3", "u3", "assistant", [{"type": "text", "text": "New branch result"}]),
    ]


def codex_user(*blocks: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text} for text in blocks],
        },
    }


AGENTS_SCAFFOLD = (
    "# AGENTS.md instructions for /Users/example/project\n\n"
    "<INSTRUCTIONS>\nSYNTH_AGENTS_RULE use careful tone\n</INSTRUCTIONS>"
)


# Older Codex builds omit the "for <path>" part of the header.
AGENTS_SCAFFOLD_BARE = "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nSYNTH_AGENTS_RULE\n</INSTRUCTIONS>"


ENV_CONTEXT = "<environment_context>\n  <cwd>/Users/example/project</cwd>\n</environment_context>"


def codex_meta(session_id: str, cwd: str = "/Users/alex/projects/demo", **extra) -> dict:
    return {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd, **extra}}


def codex_answer(text: str, timestamp: str = "2026-09-22T10:00:05Z") -> dict:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def datetime_from_iso(value: str):

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fake(*parts: str) -> str:
    """Secret-shaped test values are assembled at run time, so no literal in
    this repository looks like a real credential to secret scanners."""
    return "".join(parts)


# Invented values in the shapes the redactor has to catch.
FAKE = {
    "github": fake("gh", "p_", "Zq3" * 12),
    "openai": fake("sk", "-proj-", "Ab3dE" * 6),
    "aws": fake("AK", "IA", "QWERTYUIOPASDFGH"),
    "jwt": fake("ey", "JhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiJ0ZXN0MTIzIn0", ".", "c2lnbmF0dXJlX3Rlc3Q"),
    "bearer": fake("Tq8", "wLm2", "Zx9Kp4", "Rs7Vb"),
    "urlpass": fake("Pw", "4dbUser", "Secret"),
    "envpass": fake("hunter", "2hunter2"),
    "jsonkey": fake("live", "-key-", "value-77"),
    "flag": fake("flag", "Token", "Value9"),
    "sshpass": fake("ssh", "Pass", "Word5"),
    "random": fake("aZ3kQ9", "mN2pX7", "vB5tR1", "cL8wY4", "hJ6s"),
    "pem": fake("MIIEow", "IBAAKCAQEA", "fakefakefake"),
}


PEM_EDGE, PEM_WORDS = "-" * 5, fake("PRIVATE", " KEY")


SECRET_TEXT = (
    f"export GITHUB_TOKEN={FAKE['github']}; key {FAKE['openai']} aws {FAKE['aws']} jwt {FAKE['jwt']} "
    f'curl -H "Authorization: Bearer {FAKE["bearer"]}" postgres://app:{FAKE["urlpass"]}@db.internal/x '
    f'DB_PASSWORD={FAKE["envpass"]} {{"api_key": "{FAKE["jsonkey"]}"}} --token {FAKE["flag"]} '
    f"sshpass -p {FAKE['sshpass']} ssh host; blob {FAKE['random']} "
    f"{PEM_EDGE}BEGIN RSA {PEM_WORDS}{PEM_EDGE} {FAKE['pem']} {PEM_EDGE}END RSA {PEM_WORDS}{PEM_EDGE}"
)


# Things that look technical but must stay searchable.
KEPT = (
    "0b7c2f1e-5d1a-4c55-9a43-2f1d8e0c9b11",  # session id / UUID
    "4f1c9e0b7a2d6e8f3b5c1a9d7e2f4b6c8a0d3e1f",  # commit hash
    "q7kd2mx9wfzt4pnb8rcy3hle",  # lowercase deployment id
    "$GITHUB_TOKEN",  # a reference, not a value
    "-Users-alex-projects-shop-2026",  # encoded project directory
    "~/projects/shop/.env",
)


def claude_rows(cwd: str = "/Users/alex/projects/shop") -> list[dict]:
    return [
        {
            "timestamp": "2026-09-22T10:00:00Z",
            "cwd": cwd,
            "message": {"role": "user", "content": f"Deploy the shop. Here are the keys: {SECRET_TEXT}"},
        },
        {
            "timestamp": "2026-09-22T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"I will use {FAKE['openai']} and keep {' '.join(KEPT)}."},
                ],
            },
        },
        {
            "timestamp": "2026-09-22T10:00:06Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": f"DEPLOYCMD --env prod {SECRET_TEXT}", "description": "deploy"},
                    },
                ],
            },
        },
        {
            "timestamp": "2026-09-22T10:00:07Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "TOOLOUTPUTWORD done"},
                ],
            },
        },
        {
            "timestamp": "2026-09-22T10:00:08Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "Write",
                        "input": {"file_path": "/Users/alex/projects/shop/WRITTENFILE.md", "content": "FILEBODYWORD"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_3",
                        "name": "Edit",
                        "input": {
                            "file_path": "/Users/alex/projects/shop/a.py",
                            "old_string": "OLDBODY",
                            "new_string": "NEWBODY",
                        },
                    },
                ],
            },
        },
        # Claude Code can write the same call twice while streaming.
        {
            "timestamp": "2026-09-22T10:00:08Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "Write",
                        "input": {"file_path": "/Users/alex/projects/shop/WRITTENFILE.md", "content": "FILEBODYWORD"},
                    },
                ],
            },
        },
        {
            "timestamp": "2026-09-22T10:00:09Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_4",
                        "name": "Bash",
                        "input": {
                            "command": 'python3 ~/.local/share/find-session/scripts/sessions.py search "LOOKUPTOPIC"'
                        },
                    },
                ],
            },
        },
        {
            "timestamp": "2026-09-22T10:01:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Deployed."}]},
        },
    ]


def codex_tool_rows() -> list[dict]:
    return [
        codex_meta("codex-tools", cwd="/Users/alex/projects/shop"),
        codex_user("Ship the release"),
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": json.dumps({"cmd": "CODEXFUNCCMD --release 2.1", "max_output_tokens": 1000}),
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c1", "output": "CODEXOUTPUTWORD"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c2",
                "input": 'text(await tools.exec_command({cmd: "CODEXEXECCMD --dry-run", yield_time_ms: 1000}))',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "c3",
                "input": "*** Begin Patch\n*** Update File: src/PATCHEDFILE.py\n+PATCHBODYWORD\n*** End Patch",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "c4",
                "arguments": '{"timeout_ms": 5}',
            },
        },
        codex_answer("Released."),
    ]


def claude_turns(*texts: str, start: str = "2026-09-22T10:00:00Z") -> list[dict]:
    rows = []
    base_time = datetime_from_iso(start)
    for index, text in enumerate(texts):
        role = "user" if index % 2 == 0 else "assistant"
        content = text if role == "user" else [{"type": "text", "text": text}]
        stamp = base_time.replace(minute=index % 60).isoformat().replace("+00:00", "Z")
        rows.append({"timestamp": stamp, "cwd": "/Users/alex/work", "message": {"role": role, "content": content}})
    return rows


def make_omp_home(tmp: str) -> Path:
    """A synthetic omp history: one main session with a subagent, a nested one and an advisor."""
    home = Path(tmp)
    main = home / OMP_BUCKET / f"{MAIN_STEM}.jsonl"
    write_omp(
        main,
        main_omp_rows(),
        tail='{"type":"message","id":"zz","parentId":"a3","message":{"role":"user","content":"PARTI',
    )
    artifacts = main.with_suffix("")
    write_omp(
        artifacts / "Task.jsonl",
        [
            omp_header("child-header-id"),
            omp_msg("u1", None, "user", "Child task CHILDTOPIC"),
            omp_msg("a1", "u1", "assistant", [{"type": "text", "text": "Child result"}]),
        ],
    )
    write_omp(
        artifacts / "Task" / "Sub.jsonl",
        [omp_header("nested"), omp_msg("u1", None, "user", "Grandchild CHILDTOPIC")],
    )
    (artifacts / "Task.md").write_text("SYNTH_AGENT_OUTPUT")
    (artifacts / "0.bash.log").write_text("SYNTH_LOG")
    write_omp(
        artifacts / "__advisor.jsonl",
        [omp_header("advisor"), omp_msg("u1", None, "user", "ADVISOR CHILDTOPIC")],
    )
    return home
