"""Agent loop: Claude writes a Processing sketch, runs it, looks at the frames, and iterates."""

import base64
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from .runner import run_sketch

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
MAX_RUNS_PER_TURN = int(os.environ.get("MAX_RUNS_PER_TURN", "6"))

SYSTEM_PROMPT = """\
You are a creative coder who turns text descriptions into Processing (Java mode, Processing 4) sketches.

Workflow:
1. Write a complete sketch and call `run_sketch` with it. You'll get back the console output and \
the captured frames as images.
2. Look at the frames critically against the user's description. If it doesn't compile, crashes, \
or doesn't look right, fix it and run again. Stop once it's a good match; don't polish endlessly.
3. Finish with a short note (1-3 sentences) on what you made and any notable techniques. Don't \
repeat the code in your reply; the user sees the code from your last run.

Sketch rules:
- Always use `void setup()` (and `void draw()` if animated). Call `size()` first in setup().
- Use only the core Processing API, no external libraries, files, fonts, images, or network access.
- Use the default renderer (no P2D/P3D) unless the user asks for 3D: OpenGL sketches open a window on \
the user's screen while they run.
- Keep it self-contained in one file and reasonably fast; each frame should render well under a second.
- For animations, choose `capture_frames` that show how it evolves (e.g. [1, 60, 180]) and set \
`video_seconds` (typically 4-6) so the user gets a video clip. The clip is for the user; you judge \
motion from the still frames. Clips can be up to 60 s when the user asks for a long or looping video, \
but recording takes about 10 s per second of video for heavy sketches, so iterate with short clips \
and record the long one on the final run. For a seamless loop, make the animation repeat every \
video_seconds * 60 frames (the clip records 60 sketch frames per second of video). For static images, call noLoop(), capture [1] and set video_seconds to 0.
- The limits above are current. The tool gets updated, so they override any limits you saw or \
mentioned earlier in this conversation: when in doubt, try the run rather than refusing.
- Drive animation from frameCount rather than millis(); recording slows the sketch down in real time.
- Default canvas around 800x600 unless the description implies otherwise.

When the user asks for changes in a follow-up, modify the previous sketch rather than starting over \
unless they ask for something new.
"""

TOOLS = [
    {
        "name": "run_sketch",
        "description": (
            "Compile and run a Processing (Java mode) sketch headlessly. Captures the given frame "
            "numbers as PNGs and returns them as images along with compiler/console output. "
            "Errors reference the sketch as sketch.pde with line numbers."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Full contents of the .pde sketch."},
                "capture_frames": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Frame numbers to capture (1-based). Use [1] for static sketches.",
                },
                "video_seconds": {
                    "type": "number",
                    "description": "Length of the video clip to record for the user (0-60). 0 for static sketches.",
                },
            },
            "required": ["code", "capture_frames", "video_seconds"],
            "additionalProperties": False,
        },
    }
]


@dataclass
class Session:
    id: str
    dir: Path
    messages: list = field(default_factory=list)
    run_count: int = 0
    latest_code: str | None = None
    cc_session_id: str | None = None  # Claude Code backend only


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


class Agent:
    def __init__(self) -> None:
        self.client: anthropic.AsyncAnthropic | None = None
        self._api_key: str | None = None

    def set_api_key(self, key: str) -> None:
        if key != self._api_key:
            self.client = anthropic.AsyncAnthropic(api_key=key)
            self._api_key = key

    async def _call(self, session: Session):
        return await self.client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=session.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": EFFORT},
            cache_control={"type": "ephemeral"},
            # Re-run safety-classifier refusals on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    async def run_turn(self, session: Session, prompt: str) -> AsyncIterator[dict]:
        """Run one user turn, yielding UI events as the agent works."""
        session.messages.append({"role": "user", "content": prompt})
        runs_this_turn = 0

        while True:
            yield {"type": "status", "text": "Claude is thinking…"}
            try:
                response = await self._call(session)
            except anthropic.APIStatusError as e:
                yield {"type": "error", "text": f"API error {e.status_code}: {e.message}"}
                return
            except anthropic.APIConnectionError as e:
                yield {"type": "error", "text": f"Connection error: {e}"}
                return

            if response.stop_reason == "refusal":
                yield {"type": "error", "text": "Claude declined this request."}
                return

            session.messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "thinking" and block.thinking:
                    yield {"type": "thinking", "text": block.thinking}
                elif block.type == "text" and block.text.strip():
                    yield {"type": "text", "text": block.text}

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                if response.stop_reason == "max_tokens":
                    yield {"type": "error", "text": "Response hit the token limit."}
                yield {"type": "done"}
                return

            results = []
            for tool in tool_uses:
                async for item in self._run_tool(session, tool, runs_this_turn):
                    if item["type"] == "tool_result":
                        results.append(item)
                    else:
                        yield item
                runs_this_turn += 1
            session.messages.append({"role": "user", "content": results})

    async def _run_tool(self, session: Session, tool, runs_this_turn: int) -> AsyncIterator[dict]:
        """Yields UI events, then the tool_result block as the last item."""
        inp = tool.input if isinstance(tool.input, dict) else json.loads(tool.input)
        code, frames = inp.get("code"), inp.get("capture_frames") or [1]
        video_seconds = inp.get("video_seconds") or 0

        if tool.name != "run_sketch" or not isinstance(code, str):
            yield {"type": "tool_result", "tool_use_id": tool.id, "is_error": True,
                   "content": "Invalid tool call: expected run_sketch with a `code` string."}
            return
        if runs_this_turn >= MAX_RUNS_PER_TURN:
            yield {"type": "tool_result", "tool_use_id": tool.id, "is_error": True,
                   "content": f"Run limit ({MAX_RUNS_PER_TURN}) reached for this request. "
                              "Stop and summarize the current state for the user."}
            return

        session.run_count += 1
        run_id = f"run-{session.run_count:02d}"
        yield {"type": "running", "run": run_id, "code": code}

        result = await run_sketch(session.dir / run_id / "sketch", code, frames[:6], video_seconds)

        frame_urls = [f"/runs/{session.id}/{run_id}/sketch/__frames/{p.name}" for p in result.frames]
        if result.ok:
            session.latest_code = code
        yield {
            "type": "result", "run": run_id, "ok": result.ok, "code": code,
            "output": result.output, "frames": frame_urls, "timed_out": result.timed_out,
            "video": f"/runs/{session.id}/{run_id}/video.mp4" if result.video else None,
        }

        text = result.summary()
        content = [{"type": "text", "text": text}]
        for p in result.frames:
            content.append({"type": "text", "text": f"Frame {int(p.stem.split('-')[1])}:"})
            content.append(_image_block(p))
        yield {"type": "tool_result", "tool_use_id": tool.id, "content": content,
               "is_error": not result.ok}
