# Processing Agent

Describe an image in plain English. Claude writes a [Processing](https://processing.org/) sketch for it, compiles and runs it, **looks at the rendered frames**, and keeps fixing it until it matches. Animated sketches come back as video. Follow-up messages refine the same sketch ("make the stars twinkle", "warmer colors"), and every result is saved as a version you can go back to.

```
you: "a flow field of thin orange lines over deep navy"
  └─ Claude ──run_sketch(code, frames)──▶ Processing (windowless run)
        ▲                                     │
        └──── console output + PNG frames ◀───┘   (repeats until it looks right)
```

It runs on your own Mac with your own [Claude Code](https://claude.com/claude-code) login: no API key, and nothing you make leaves your machine unless you turn on a backup.

## Set it up with Claude Code

Open Claude Code in any folder and say:

```
setup https://github.com/madeleine-charity/processing-agent
```

Claude will walk you through everything until the app is running at http://localhost:8000 and you've made your first sketch.

You'll need a Mac, [Claude Code](https://claude.com/claude-code) (logged in), and Processing 4.4 or newer (setup can install it for you). Usage counts against your Claude plan.

## Instructions for Claude Code: guided setup

If the user asked you to set up this repo, guide them through these steps in order. Explain each step briefly before doing it, check that it worked, and don't move on until it has. Ask before installing anything or changing anything outside the clone.

1. **Check the machine.** This only runs on macOS (`sw_vers`). On Linux or Windows, stop and explain that.
2. **Clone the repo.** Ask where to put it (suggest `~/projects/processing-agent`), then `git clone https://github.com/madeleine-charity/processing-agent <folder>` and work from that folder. Read `CLAUDE.md` there before going further.
3. **Processing 4.4+.** Look for `/Applications/Processing.app` and read its version (`defaults read /Applications/Processing.app/Contents/Info.plist CFBundleShortVersionString`). If it's missing or older, offer `brew install --cask processing` if Homebrew is installed, or send them to https://processing.org/download and wait. Processing installed elsewhere works with `PROCESSING_APP=<path to Processing.app>` in `.env`.
4. **Python 3.10+** (`python3 --version`). If missing, offer `brew install python` or https://www.python.org/downloads/.
5. **Install dependencies:** `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt`.
6. **Settings:** `cp .env.example .env`. Leave `ANTHROPIC_API_KEY` empty so the app uses their Claude Code login. Leave everything else as is unless they ask.
7. **Claude Code login:** `claude --version` must work. You're running in Claude Code, so it's installed; if `claude` isn't on the PATH for subprocesses, set `CLAUDE_BIN=<path>` in `.env`.
8. **Test it:** `.venv/bin/pytest`. The runner tests compile and render real sketches without opening any window, so every test must pass. A failure here usually means Processing's install differs from what `app/runner.py` expects (see "If something fails").
9. **Start the server** in the background with `./start.sh`, then wait until `curl -s localhost:8000/api/status` answers.
10. **First sketch.** Ask them to open http://localhost:8000, click **+ New sketch** and describe an image, for example "a flow field of thin orange lines over deep navy". It takes a minute or two. Watch the progress with `curl -s -N -m 5 "localhost:8000/api/projects/<slug>/events?after=0"` (the slug is in `curl -s localhost:8000/api/projects`). Setup is done when a `version` event arrives and they can see the image. Check that no window popped up on their screen.
11. **Optional extras**, only if they want them: back up their sketches to a private GitHub repo (`AUTO_PUSH=1`, needs the `gh` CLI logged in), or publish a public gallery (see "Public gallery").

Tell them the server stops when the Claude Code session that started it ends; next time they can say "run the server" in this folder.

### If something fails

First decide whether it's their machine (missing app, no network, old Python) or this repo (a bug, or a Processing or library version the code doesn't handle).

- **Their machine:** explain what's wrong and help fix it.
- **This repo:** fix it in their clone, then run `.venv/bin/pytest` and finish the setup to prove the fix works. Then ask the user whether to send the fix upstream. If they agree:
  1. `gh auth status` (if needed, they can log in with `gh auth login`),
  2. `gh repo fork --remote --remote-name fork`, then `git checkout -b fix/<short-name>`, commit only the fix, and `git push -u fork HEAD`,
  3. `gh pr create --repo madeleine-charity/processing-agent` with what failed (the exact error), the fix, how you tested it, and their macOS version, chip (`uname -m`) and Processing version.

  Never include their `.env`, `sketches/`, `runs/` or anything personal in the PR.

## Using it

- **+ New sketch** opens a blank chat; your first message creates the project, named after the prompt. Each project has its own chat, and they run independently: start a request in one, switch to another, come back.
- While Claude works you see what it's doing (thinking, writing the sketch, rendering attempt N) and a timer. Click any run in the chat to see it again; click an image to open it full size.
- Each finished request that changes the code is saved as a new **version**, with a thumbnail in the version strip. **Restore** makes an old version current again (as a new version, so nothing is lost). **Fork** starts a new project from any version.
- **Run live in Processing** opens the sketch in a real window; **Download .pde** saves it.
- Ask for a long or looping video ("make it a slow 40-second seamless loop") to get clips up to 60 s. Long clips of detailed sketches can take several minutes to record.

## Your data stays on your machine

Nothing you make is part of this repo:

- `sketches/` is your sketches **and their version history**: its own git repo, ignored by this one. Each version is a commit titled `[<name>] vN: <your prompt>` and tagged `<name>/vN`, holding `sketch/sketch.pde` (opens directly in Processing), a small `thumb.jpg` and a `README.md`. It's created the first time the server starts.
- `runs/` holds the chats, every attempt, and each version's full-size images and video (`runs/<name>/media/<version>/`).
- **Backup (optional):** with `AUTO_PUSH=1`, every new version, restore and fork is pushed in the background to a private GitHub repo `processing-sketches` under your account, created with `gh` the first time. Full-size images and video stay local; the backup has the code and thumbnails.
- **Clear chat** starts a fresh conversation in a project (its versions are kept) and deletes its old attempts from `runs/`.
- If the server restarts mid-request, the request is marked as interrupted, and any version it had already written is saved on the next start.

## No pop-up windows

Sketches render without a window. The runner compiles with `Processing cli --build`, then launches the JVM itself with `-Dapple.awt.UIElement=true`, which keeps it out of the Dock and from taking focus. An injected tab swaps Processing's window for its built-in windowless surface (`PSurfaceNone`). The one exception is P2D/P3D (OpenGL) sketches, which need a real window, so Claude is told to use the default renderer unless you ask for 3D.

## Backends

- **Claude Code (default, used when `ANTHROPIC_API_KEY` is empty):** each request runs `claude -p` with your Claude Code login. Its built-in tools are disabled; its only tool is `run_sketch`, served by `app/mcp_server.py` over MCP. Follow-ups resume the same Claude Code session.
- **Anthropic API:** set `ANTHROPIC_API_KEY` in `.env` (picked up on the next request, no restart). Uses `app/agent.py` directly.

## Public gallery (optional)

`app/gallery.py` can publish every new version to a static site on Vercel that shows each prompt with the image or video Claude made and Claude's reply. Example: https://processing-gallery.vercel.app. It needs a folder holding the site (an `index.html` that reads `content/gallery.json`), linked to a Vercel project, at `GALLERY_DIR` (default `../processing-gallery`). It only turns on when that folder exists and is linked (`.vercel/`). Publish by hand with `.venv/bin/python -m app.gallery`; set `AUTO_PUBLISH=0` to turn it off.

## How it works

- **`app/agent.py`**: the system prompt and a manual tool-use loop for the API backend (`claude-opus-5-5`, adaptive thinking). Claude has a single tool, `run_sketch(code, capture_frames, video_seconds)`. Each result comes back as compiler/console text plus the captured frames as images, so Claude can judge the visual result and iterate. Each request is capped at `MAX_RUNS_PER_TURN` runs.
- **`app/claude_code_agent.py`**: runs `claude -p` and turns its stream into UI events, including "Writing the sketch (N lines)" progress.
- **`app/runner.py`**: writes the sketch to `runs/<name>/run-NN/sketch/`, injects a hidden tab that renders offscreen and saves the requested frames, compiles it with `Processing cli --build`, and runs it in a windowless JVM. Static-mode sketches (no functions) get wrapped in `setup()`. For animated sketches it also saves every 2nd frame and encodes a looping MP4 (30 fps) with the ffmpeg bundled in `imageio-ffmpeg`. Compile errors, runtime exceptions and timeouts are detected and the whole process group is killed.
- **`app/projects.py`**: projects, versions (commits and tags in the sketches repo; media kept locally), restore, fork, backup push, restart recovery, and background agent turns.
- **`app/gallery.py`**: the optional public gallery.
- **`app/server.py`**: FastAPI app. `POST /api/projects/<name>/prompt` starts a turn; `GET /api/projects/<name>/events` streams its events over SSE.
- **`static/index.html`**: the whole UI. Vanilla JS, no build step.
- **`tests/`**: `.venv/bin/pytest`. They use throwaway repos and never touch your sketches.

## Configuration (`.env`)

| Var | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | set to use the API backend instead of Claude Code |
| `CLAUDE_CODE_MODEL` | your Claude Code default | model for the Claude Code backend |
| `CLAUDE_MODEL` | `claude-opus-5-5` | model for the API backend |
| `CLAUDE_EFFORT` | `medium` | both backends; `low` is faster and cheaper, `high` takes more care |
| `AUTO_PUSH` | `0` | `1` backs up your sketches to a private GitHub repo after every new version |
| `AUTO_PUBLISH` | `1` | publish to the gallery, if one is set up; `0` turns it off |
| `GALLERY_DIR` | `../processing-gallery` | the gallery site folder (must be linked to Vercel) |
| `SKETCHES_DIR` | `./sketches` | where your sketches repo lives |
| `MAX_RUNS_PER_TURN` | `6` | max sketch runs per request |
| `SKETCH_TIMEOUT` | `60` | seconds before a run is killed, plus 10 s per second of video. A run that runs out of time while recording, after its still frames, keeps a shorter clip |
| `PROCESSING_APP` | `/Applications/Processing.app` | where Processing is installed |
| `CLAUDE_BIN` | `claude` on your PATH | path to the Claude Code CLI |

## Notes

- Recording video adds roughly 5-10 seconds to a short clip; a long clip of a heavy sketch can take about 10 seconds per second of video. Animations driven by `millis()` instead of `frameCount` look sped up in the clip, because saving frames slows the sketch down in real time.
- Sketches are AI-generated Java running on your machine with your user permissions. The system prompt forbids files and network access, but nothing enforces that. Treat this as a local tool.

## Contributing

Bug fixes are welcome as pull requests, especially for setups that didn't work (a different Processing version, Intel Macs, and so on). Run `.venv/bin/pytest` before sending one.

## License

[MIT](LICENSE)
