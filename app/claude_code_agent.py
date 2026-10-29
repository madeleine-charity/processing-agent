"""Agent backend that drives the local `claude` CLI (your Claude Code login, no API key).

Each user turn runs `claude -p` with only our `run_sketch` MCP tool available, streams its
JSON events, and translates them into the same UI events as the API backend.
"""

import asyncio
import json
import os
import re
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from .agent import EFFORT, MAX_RUNS_PER_TURN, SYSTEM_PROMPT, Session

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
CLAUDE_CODE_MODEL = os.environ.get("CLAUDE_CODE_MODEL")  # unset = your Claude Code default
TOOL_NAME = "mcp__processing__run_sketch"
RUN_TAG = re.compile(r"^\[(run-\d+)\]")
# While Claude writes a sketch, report progress every this many lines.
PROGRESS_LINES = 20


def _existing_runs(session: Session) -> int:
    nums = [int(m.group(1)) for p in session.dir.glob("run-*") if (m := re.fullmatch(r"run-(\d+)", p.name))]
    return max(nums, default=0)


def _result_text(block: dict) -> str:
    content = block.get("content")
    return content if isinstance(content, str) else " ".join(
        c.get("text", "") for c in content or [] if c.get("type") == "text")


def _run_from_result(block: dict) -> str | None:
    """The tool's reply starts with "[run-NN]", the folder it actually wrote."""
    m = RUN_TAG.match(_result_text(block).strip())
    return m.group(1) if m else None


class ClaudeCodeAgent:
    def _command(self, session: Session, prompt: str) -> list[str]:
        mcp_config = {"mcpServers": {"processing": {
            "command": sys.executable,
            "args": ["-m", "app.mcp_server"],
            "env": {
                "PYTHONPATH": str(ROOT),
                "SESSION_DIR": str(session.dir),
                "MAX_RUNS_PER_TURN": str(MAX_RUNS_PER_TURN),
            },
        }}}
        cmd = [
            CLAUDE_BIN, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",  # stream progress while Claude writes a long sketch
            "--effort", EFFORT,
            "--system-prompt", SYSTEM_PROMPT,
            "--tools", "",  # no built-in tools: no file, shell or web access
            "--mcp-config", json.dumps(mcp_config), "--strict-mcp-config",
            "--allowedTools", TOOL_NAME,
            "--setting-sources", "",  # skip your plugins/hooks; auth is unaffected
        ]
        if CLAUDE_CODE_MODEL:
            cmd += ["--model", CLAUDE_CODE_MODEL]
        if session.cc_session_id:
            cmd += ["--resume", session.cc_session_id]
        return cmd

    async def run_turn(self, session: Session, prompt: str) -> AsyncIterator[dict]:
        session.dir.mkdir(parents=True, exist_ok=True)
        yield {"type": "status", "text": "Claude is thinking…"}

        proc = await asyncio.create_subprocess_exec(
            *self._command(session, prompt),
            cwd=session.dir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,  # tool results with images make for long lines
        )
        pending: dict[str, str] = {}  # tool_use_id -> run id
        # The MCP server can create a run's folder before we see the tool call, so number
        # runs from a snapshot taken now rather than from the folders present later.
        base_runs = _existing_runs(session)
        started = 0
        writing_lines = None  # lines of sketch code streamed so far, while one is being written
        final_error = None

        try:
            assert proc.stdout
            async for raw in proc.stdout:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = msg.get("type")

                if kind == "stream_event":
                    ev = msg.get("event", {})
                    if ev.get("type") == "content_block_start":
                        block_type = ev.get("content_block", {}).get("type")
                        if block_type == "tool_use":
                            writing_lines = 0
                            yield {"type": "status", "text": "Writing the sketch…"}
                        elif block_type == "thinking":
                            yield {"type": "status", "text": "Claude is thinking…"}
                    elif ev.get("type") == "content_block_delta" and writing_lines is not None:
                        chunk = ev.get("delta", {}).get("partial_json", "")
                        before = writing_lines // PROGRESS_LINES
                        writing_lines += chunk.count("\\n")
                        if writing_lines // PROGRESS_LINES > before:
                            yield {"type": "status", "text": f"Writing the sketch ({writing_lines} lines)…"}
                    elif ev.get("type") == "content_block_stop":
                        writing_lines = None

                elif kind == "system" and msg.get("subtype") == "init":
                    session.cc_session_id = msg.get("session_id")

                elif kind == "assistant":
                    for block in msg.get("message", {}).get("content", []):
                        if block.get("type") == "thinking" and block.get("thinking"):
                            yield {"type": "thinking", "text": block["thinking"]}
                        elif block.get("type") == "text" and block.get("text", "").strip():
                            yield {"type": "text", "text": block["text"]}
                        elif block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
                            started += 1
                            run_id = f"run-{base_runs + started:02d}"
                            pending[block["id"]] = run_id
                            yield {"type": "status", "text": f"Compiling and running {run_id}…"}
                            yield {"type": "running", "run": run_id,
                                   "code": block.get("input", {}).get("code", "")}

                elif kind == "user":
                    for block in msg.get("message", {}).get("content", []):
                        if block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                            guess = pending.pop(block["tool_use_id"])
                            yield self._result_event(session, _run_from_result(block) or guess, block)
                            yield {"type": "status", "text": "Claude is looking at the output…"}

                elif kind == "result":
                    session.cc_session_id = msg.get("session_id") or session.cc_session_id
                    if msg.get("is_error"):
                        final_error = msg.get("result") or msg.get("subtype") or "Claude Code reported an error."
        finally:
            await proc.wait()

        if proc.returncode != 0 and not final_error:
            err = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
            final_error = err[-800:] or f"claude exited with code {proc.returncode}"
        if final_error:
            yield {"type": "error", "text": final_error}
        yield {"type": "done"}

    def _result_event(self, session: Session, run_id: str, block: dict) -> dict:
        meta_path = session.dir / run_id / "result.json"
        if not meta_path.exists():  # tool errored before running (e.g. run limit)
            return {"type": "result", "run": run_id, "ok": False, "code": "", "output": _result_text(block),
                    "frames": [], "timed_out": False, "video": None}
        meta = json.loads(meta_path.read_text())
        if meta["ok"]:
            session.latest_code = meta["code"]
        return {
            "type": "result", "run": run_id, "ok": meta["ok"], "code": meta["code"],
            "output": meta["output"], "timed_out": meta["timed_out"],
            "frames": [f"/runs/{session.id}/{run_id}/sketch/__frames/{f}" for f in meta["frames"]],
            "video": f"/runs/{session.id}/{run_id}/video.mp4" if meta.get("video") else None,
            "variant": meta.get("variant"),
        }
