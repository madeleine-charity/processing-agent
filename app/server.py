"""FastAPI app: projects, background agent turns, an SSE event feed per project, and versions."""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .agent import Agent  # noqa: E402
from .claude_code_agent import ClaudeCodeAgent  # noqa: E402
from .gallery import AUTO_PUBLISH, GALLERY_DIR, Publisher, save_replies  # noqa: E402
from .projects import (AUTO_PUSH, MEDIA_FILES, SKETCH_FILE, SKETCHES_DIR, THUMB_FILE,  # noqa: E402
                       Project, ProjectStore)
from .runner import launch_live  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
RUNS_DIR.mkdir(exist_ok=True)

api_agent = Agent()
claude_code_agent = ClaudeCodeAgent()
store = ProjectStore(SKETCHES_DIR, RUNS_DIR)
if AUTO_PUBLISH and (GALLERY_DIR / ".vercel").exists():  # the gallery folder, linked to Vercel
    store.publisher = Publisher(store.projects)

app = FastAPI(title="Processing Agent")
app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")


def _agent():
    # Use the API when a key is configured; otherwise go through your local Claude Code login.
    load_dotenv(ROOT / ".env", override=True)  # pick up key edits without a restart
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        api_agent.set_api_key(key)
        return api_agent
    return claude_code_agent


def _project(slug: str) -> Project:
    try:
        return store.get(slug)
    except KeyError:
        raise HTTPException(404, "No such project")


class NameIn(BaseModel):
    name: str


class PromptIn(BaseModel):
    prompt: str


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
async def status():
    return {"backend": "api" if os.environ.get("ANTHROPIC_API_KEY") else "claude-code"}


@app.get("/api/projects")
async def list_projects():
    projects = [p.summary() for p in store.projects.values()]
    return sorted(projects, key=lambda p: p["updated"] or "9999", reverse=True)


@app.post("/api/projects")
async def create_project(body: NameIn):
    return store.create(body.name).summary()


@app.post("/api/projects/{slug}/prompt")
async def prompt(slug: str, body: PromptIn):
    p = _project(slug)
    if not body.prompt.strip():
        raise HTTPException(400, "Empty prompt")
    if p.busy:
        raise HTTPException(409, "This project is already working on a request")
    store.start(p, body.prompt.strip(), _agent())
    return {"ok": True}


@app.get("/api/projects/{slug}/events")
async def events(slug: str, request: Request, after: int = 0):
    """SSE feed of a project's events from index `after`, then live updates."""
    p = _project(slug)

    async def stream():
        i = after
        yield f"data: {json.dumps({'type': 'hello', 'busy': p.busy})}\n\n"
        replayed = False
        while not await request.is_disconnected():
            while i < len(p.events):
                yield f"data: {json.dumps({'i': i + 1, **p.events[i]})}\n\n"
                i += 1
            if not replayed:  # the UI draws the image panel once the backlog is in
                yield f"data: {json.dumps({'type': 'replayed'})}\n\n"
                replayed = True
            try:
                async with p.changed:
                    await asyncio.wait_for(p.changed.wait(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/projects/{slug}/reset-chat")
async def reset_chat(slug: str):
    p = _project(slug)
    if p.busy:
        raise HTTPException(409, "Project is busy")
    save_replies(p)  # keep Claude's replies for the gallery before the chat goes
    store.reset_chat(p)
    return {"ok": True}


@app.get("/api/projects/{slug}/versions")
async def versions(slug: str):
    return _project(slug).versions()


@app.get("/api/projects/{slug}/versions/{sha}/{name}")
async def version_file(slug: str, sha: str, name: str):
    p = _project(slug)
    try:
        if name == "sketch.pde":
            data, media_type = p.show(sha, SKETCH_FILE), "text/plain; charset=utf-8"
        elif name in MEDIA_FILES:
            data, media_type = p.media_file(sha, name), ("image/png" if name.endswith(".png") else "video/mp4")
        elif name == THUMB_FILE:
            # Versions made before thumbnails existed fall back to the full preview.
            try:
                data, media_type = p.show(sha, THUMB_FILE), "image/jpeg"
            except RuntimeError:
                data, media_type = p.media_file(sha, "preview.png"), "image/png"
        else:
            raise HTTPException(404)
    except (ValueError, RuntimeError, OSError):
        raise HTTPException(404, "Not in this version")
    return Response(data, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.post("/api/projects/{slug}/versions/{sha}/restore")
async def restore(slug: str, sha: str):
    p = _project(slug)
    if p.busy:
        raise HTTPException(409, "Project is busy")
    try:
        return await store.restore(p, sha)
    except KeyError:
        raise HTTPException(404, "No such version")


@app.post("/api/projects/{slug}/versions/{sha}/fork")
async def fork(slug: str, sha: str, body: NameIn):
    p = _project(slug)
    try:
        return store.create(body.name, from_project=p, sha=sha).summary()
    except (StopIteration, ValueError, RuntimeError):
        raise HTTPException(404, "No such version")


@app.get("/api/sync")
async def sync():
    return {**store.sync, "auto": AUTO_PUSH,
            "gallery": store.publisher.status if store.publisher else None}


@app.post("/api/projects/{slug}/live")
async def run_live(slug: str):
    p = _project(slug)
    if not p.session.latest_code:
        raise HTTPException(404, "No working sketch yet")
    await launch_live(p.session.dir, p.session.latest_code)
    return {"ok": True}


@app.get("/api/projects/{slug}/sketch.pde")
async def download(slug: str):
    p = _project(slug)
    if not p.session.latest_code:
        raise HTTPException(404, "No working sketch yet")
    return PlainTextResponse(
        p.session.latest_code,
        headers={"Content-Disposition": f'attachment; filename="{slug}.pde"'},
    )
