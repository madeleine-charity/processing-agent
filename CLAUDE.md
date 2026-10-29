# Processing Agent

Local web app: the user describes an image, Claude writes a Processing sketch, runs it windowless, looks at the frames, iterates, and the UI shows the image/video. README.md has the user docs, the guided setup and the config.

**Setting up for a new user?** Follow "Instructions for Claude Code: guided setup" in README.md step by step, including "If something fails" (fix bugs in the repo and, with the user's OK, open a PR upstream).

Personal preferences of whoever cloned this repo may be in `CLAUDE.local.md` (gitignored). Follow them where they apply.

## Run

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN   # already running? then don't start a second one
./start.sh                         # uvicorn --reload on :8000; run it in the background
.venv/bin/pytest                   # uses throwaway repos, never the user's sketches
```

The server is tied to the session that started it, so it stops when that session ends.

Keep `--timeout-graceful-shutdown`: browsers hold SSE connections open, and without it a reload hangs forever on "Waiting for connections to close". If the server gets stuck, kill whatever listens on 8000 and restart.

**A reload kills any request in progress.** uvicorn watches every `*.py` in the repo, `tests/` included, so check that `curl -s localhost:8000/api/projects` shows nothing `busy` before editing Python. A request cut off this way is recovered on the next start: `ProjectStore._load` closes out the chat and commits any written-but-uncommitted version (the prompt is saved to `state.json` as soon as it's sent).

## Layout and data

- **This repo is only the app.** The user's work never goes in it:
  - `sketches/` (`SKETCHES_DIR`) is a separate git repo, ignored by this one: one folder per project, a commit per version (`[slug] vN: prompt`, tag `slug/vN`) holding `sketch/sketch.pde`, `README.md` and a small `thumb.jpg`. `ProjectStore` creates it on first run. All of `projects.py`'s `git()` calls run there, and version commits use a pathspec (`-- <slug>`) so they never sweep in other projects' changes.
  - `runs/` (gitignored): chats (`runs/<slug>/state.json`), every attempt (`run-NN/`), and each version's full-size frames, `preview.png`, `preview.mp4` and gallery `meta.json` in `runs/<slug>/media/<sha>/`. Media is staged in `media/pending/` and renamed to the commit sha after committing. `Project.media_file` falls back to git (older versions committed media), then to `thumb.jpg`.
- **Backups:** `AUTO_PUSH=1` pushes the sketches repo to a private GitHub repo `processing-sketches` (created with `gh`). Off by default.
- **Variants:** when one request renders several alternatives, all of them are kept: `media/<sha>/variants/<k>/` (preview.png, preview.mp4, sketch.pde) plus `variants.json`. `projects.pick_variants` picks them: runs Claude labeled with run_sketch's `variant` argument, or else a final batch of 2+ runs started in parallel (all `running` before any `result`). The app and gallery show them side by side. Before this only the last run was saved.
- **Gallery:** `app/gallery.py` publishes to a static Vercel site in `GALLERY_DIR`, only when that folder exists and is linked to Vercel. Restores and forks are skipped. Replies are saved to `meta.json` so Clear chat doesn't lose them.
- **Never commit** `.env`, `sketches/`, `runs/` or `CLAUDE.local.md` to this repo.

## Product rules

- **The UI shows image and video output only**, never sketch code. Download .pde is fine. Clicking an image opens it full size.
- **No pop-up windows:** sketches must never open a window on the user's screen. Only "Run live in Processing" does, on purpose.
- **No API key needed:** the default backend is the user's Claude Code login (`claude -p`). The API backend is used only if `ANTHROPIC_API_KEY` is set in `.env`.

## Non-obvious constraints

- **Windowless rendering** (`app/runner.py`) works by compiling with `Processing cli --build`, then running the JVM ourselves with `-Dapple.awt.UIElement=true`. The injected `zz_capture.pde` overrides `initSurface()` to use `PSurfaceNone`. Paths into Processing.app (`Contents/app/resources/jdk`, `core/library`) are specific to Processing 4.4; a new Processing version that moves them breaks the runner tests first.
  - True headless (`java.awt.headless`) doesn't work: `ShimAWT.initRun` exits.
  - `setVisible(false)` and Docker were tried and abandoned.
  - P2D/P3D need a real window, so the system prompt tells Claude to use the default renderer.
- **Static-mode sketches** (no functions) can't coexist with the injected tab, so `prepare_sketch` wraps them in `setup()`.
- **Runtime exceptions** leave the JVM running. The runner kills the process group as soon as "Exception" is printed.
- **Video:** every 2nd frame at 30 fps, up to 60 s. Animations must be driven by `frameCount`, not `millis()`. Heavy sketches record at about 5 saved frames a second, so a run's time limit is `SKETCH_TIMEOUT` + 10 s per video second; running out of time mid-recording keeps a shorter clip and the stills it reached instead of failing the run (Claude is told which frames were missed).
- **Frames sent to Claude are scaled** (`runner.frame_for_model`: JPEG, 1568px long edge, label with the real size). Full-size frames of big canvases are 5-15 MB PNGs, over the API's image limit: Claude saw no image, called it a crash and cut detail. Retina doubles frame size (`size(400, 300)` saves 800x600) unless the sketch sets `pixelDensity(1)`.
- **The system prompt's "Checking your work" and "Common bugs"** come from mistakes seen in real chats (NaN from `pow`, near-identical per-cell seeds, invisible same-color shapes, slow big canvases). Add to them when a new mistake keeps recurring.
- **mcp 2.x** renamed FastMCP: use `from mcp.server.mcpserver import MCPServer`.
- **Run numbering (Claude Code backend):** the MCP server can create `run-NN/` before the stream shows the tool call, so the run id comes from the tool reply's `[run-NN]` prefix, not from counting folders.
- **Resumed conversations remember old limits.** Follow-ups `--resume` the Claude Code session, so after changing what `run_sketch` can do, Claude may refuse based on limits it saw earlier. The system prompt says its stated limits override earlier ones; keep the prompt and tool descriptions (`agent.py` `SYSTEM_PROMPT`/`TOOLS`, `mcp_server.py`) in sync with `runner.py`.
- **Speed:** `claude -p` gets `--effort $CLAUDE_EFFORT` (default `medium`) and `--include-partial-messages`, which drives the "Writing the sketch (N lines)" status. Big prompts still take minutes; Claude writes the whole sketch in one tool call.
- **UI:** `static/index.html` is one file of vanilla JS with no build step. The global `[hidden]{display:none !important}` rule matters, because other `display` rules otherwise override `hidden`.
  - Busy state is driven by the `user` → `idle` event bracket in the SSE replay, not by `hello.busy` alone. That avoids a race.
  - The events feed sends a `replayed` marker after a project's history. Until then the UI rebuilds only the chat; it then shows the latest version once, so opening a project doesn't flicker through old runs. Events carry an index `i`, and the UI skips ones it has seen, so a reconnecting EventSource doesn't duplicate the chat.
  - While a run renders, only a small pill sits in the corner; the last image or video stays visible and playing (the user asked for no blur).
  - The sidebar and version strip load `thumb.jpg`, never the full PNG (some are 10 MB).

## Testing

- **Don't send test prompts to the user's real projects on port 8000.** They create real versions, and pushes or gallery posts if those are on. Use `pytest`, a scratch copy of the repo on another port with `AUTO_PUSH=0 AUTO_PUBLISH=0 SKETCHES_DIR=<scratch dir>`, or simulate UI state from the browser (for example, call `setBusy(true)` and `showRendering('run-01')` in the page).
- **UI checks:** headless Chrome (`--headless=new --remote-debugging-port=...`) driven over CDP with the `websockets` package already in the venv. Take screenshots and look at them.
- **Window check:** confirm no on-screen window appears during a run.
- **Quick checks:** `.venv/bin/python -c "import app.server"`, and `node --check` on the `<script>` extracted from `static/index.html`.
