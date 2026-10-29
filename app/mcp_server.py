"""MCP server exposing `run_sketch`, for the Claude Code backend (no API key needed).

Launched by `claude -p` per user turn with SESSION_DIR pointing at runs/<session>.
Each run is saved to SESSION_DIR/run-NN/ along with a result.json the web server reads.
"""

import json
import os
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image

from .runner import frame_for_model, run_sketch as run

SESSION_DIR = Path(os.environ["SESSION_DIR"])
MAX_RUNS_PER_TURN = int(os.environ.get("MAX_RUNS_PER_TURN", "6"))

server = MCPServer("processing")
runs_this_turn = 0


def next_run_id() -> str:
    nums = [int(m.group(1)) for p in SESSION_DIR.glob("run-*") if (m := re.fullmatch(r"run-(\d+)", p.name))]
    return f"run-{max(nums, default=0) + 1:02d}"


@server.tool()
async def run_sketch(code: str, capture_frames: list[int], video_seconds: float = 0,
                     variant: str = "") -> list[str | Image]:
    """Compile and run a Processing (Java mode) sketch headlessly. Captures the given frame
    numbers (1-based; use [1] for static sketches) as PNGs and returns them as images along
    with compiler/console output. Errors reference the sketch as sketch.pde with line numbers.
    video_seconds (0-60) records a video clip of an animated sketch for the user; use 0 for
    static sketches. variant: a short label (e.g. "A: Whisper") when this run is one of several
    alternatives for the user to compare; every labeled result is shown side by side. Leave it
    empty for ordinary attempts and fixes."""
    global runs_this_turn
    if runs_this_turn >= MAX_RUNS_PER_TURN:
        return [f"Run limit ({MAX_RUNS_PER_TURN}) reached for this request. "
                "Stop and summarize the current state for the user."]
    runs_this_turn += 1

    run_id = next_run_id()
    run_dir = SESSION_DIR / run_id
    result = await run(run_dir / "sketch", code, (capture_frames or [1])[:6], video_seconds)
    (run_dir / "result.json").write_text(json.dumps({
        "ok": result.ok, "output": result.output, "timed_out": result.timed_out, "code": code,
        "frames": [p.name for p in result.frames], "video": bool(result.video),
        "variant": variant.strip() or None,
    }))

    content: list[str | Image] = [f"[{run_id}] " + result.summary()]
    for p in result.frames:
        view, label = frame_for_model(p)
        content.append(label)
        content.append(Image(path=view))
    return content


if __name__ == "__main__":
    server.run("stdio")
