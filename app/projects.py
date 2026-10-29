"""Projects: each one is a folder <slug>/ in the sketches repo (SKETCHES_DIR, a git repo of
its own that the app repo ignores), with a version (a commit touching only that folder, tagged
<slug>/vN) per finished request. Agent turns run as background tasks so several projects can
work at once; the UI follows a project's event log over SSE and can switch away and back
without losing it.

The sketches repo holds each version's code, README and a small thumb.jpg. Full-size frames and
video stay local in runs/<slug>/media/<sha>/, so heavy files never get pushed. Older versions
may still have their media in git, which media_file() falls back to.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg

from .agent import TOOL_CHANGES, TOOL_VERSION, Session

GIT_NAME = "Processing Agent"
SKETCH_FILE = "sketch/sketch.pde"
THUMB_FILE = "thumb.jpg"
# Kept out of git; older versions may still have these committed.
MEDIA_FILES = ("preview.png", "preview.mp4")
# A version made of several alternatives keeps each one in media/<sha>/variants/<k>/.
VARIANTS_FILE = "variants.json"
VARIANT_FILES = (*MEDIA_FILES, "sketch.pde")
REPO_ROOT = Path(__file__).resolve().parent.parent
# Your sketches and their versions: a separate git repo, so they never go into the app's repo.
SKETCHES_DIR = Path(os.environ.get("SKETCHES_DIR", REPO_ROOT / "sketches"))
GITHUB_REPO_NAME = "processing-sketches"
# AUTO_PUSH=1 pushes the sketches repo to a private GitHub repo after every new version.
AUTO_PUSH = os.environ.get("AUTO_PUSH", "0") == "1"


def _git_email() -> str:
    out = subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True)
    return out.stdout.strip() or "processing-agent@localhost"


GIT_EMAIL = _git_email()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return slug or "sketch"


def git(*args: str, check: bool = True, binary: bool = False):
    out = subprocess.run(
        ["git", "-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}", *args],
        cwd=SKETCHES_DIR, capture_output=True, check=False,
    )
    if check and out.returncode != 0:
        raise RuntimeError(out.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return out.stdout if binary else out.stdout.decode(errors="replace")


@dataclass
class Project:
    slug: str
    dir: Path
    session: Session
    name: str = ""
    events: list[dict] = field(default_factory=list)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None
    # Set after restore/fork, or when the conversation was lost: the next prompt
    # then includes the current code so Claude starts from the right version.
    needs_context: bool = False

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def state_path(self) -> Path:
        # Chat/session state is local only (runs/ is gitignored).
        return self.session.dir / "state.json"

    @property
    def rel(self) -> str:
        return self.slug

    @property
    def media_root(self) -> Path:
        return self.session.dir / "media"

    # --- git -------------------------------------------------------------

    def versions(self) -> list[dict]:
        log = git("log", "--format=%H%x1f%s%x1f%cI", "--", self.rel, check=False).strip()
        result = []
        prefix = re.escape(f"[{self.slug}] ")
        for line in log.splitlines():
            sha, subject, date = line.split("\x1f")
            m = re.match(prefix + r"v(\d+): (.*)", subject)
            if not m:
                continue
            video = (self.media_root / sha / "preview.mp4").exists() or \
                "preview.mp4" in git("ls-tree", "--name-only", f"{sha}:{self.rel}").split()
            result.append({"n": int(m.group(1)), "sha": sha, "message": m.group(2), "date": date,
                           "video": video, "variants": self.variants(sha)})
        return result

    def variants(self, sha: str) -> list[dict]:
        """A version's alternatives ({"label", "video"} each, numbered from 1), or []."""
        path = self.media_root / sha / VARIANTS_FILE
        return json.loads(path.read_text()) if path.exists() else []

    def variant_file(self, sha: str, k: int, name: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha) or name not in VARIANT_FILES:
            raise ValueError("bad variant file")
        return self.media_root / sha / "variants" / str(k) / name

    def current_code(self) -> str | None:
        p = self.dir / SKETCH_FILE
        return p.read_text() if p.exists() else None

    def show(self, sha: str, path: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise ValueError("bad version id")
        return git("show", f"{sha}:{self.rel}/{path}", binary=True)

    def media_file(self, sha: str, name: str) -> bytes:
        """A version's preview.png/preview.mp4: local copy first, then git for old versions."""
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise ValueError("bad version id")
        local = self.media_root / sha / name
        if local.exists():
            return local.read_bytes()
        try:
            return self.show(sha, name)
        except RuntimeError:
            # On a fresh clone the local media is gone; the committed thumbnail stands in.
            if name == "preview.png":
                return self.show(sha, THUMB_FILE)
            raise

    def commit_version(self, code: str, message: str, stills: list[Path], video: Path | None,
                       variants: list[dict] | None = None) -> dict:
        """Commit the code and a thumbnail as a new version; the still frames (the last one is
        the preview) and the video clip are kept locally for that version. `variants` are
        alternatives made by the same request ({"label", "code", "stills", "video"} each),
        kept locally next to it so they can be shown side by side."""
        (self.dir / "sketch").mkdir(parents=True, exist_ok=True)
        (self.dir / SKETCH_FILE).write_text(code)
        files = [(f, f"frames/{f.name}") for f in stills]
        if stills:
            files.append((stills[-1], "preview.png"))
        if video and video.exists():
            files.append((video, "preview.mp4"))
        listed = []
        for k, var in enumerate(variants or [], 1):
            if var["stills"]:
                files.append((var["stills"][-1], f"variants/{k}/preview.png"))
            has_video = bool(var["video"] and var["video"].exists())
            if has_video:
                files.append((var["video"], f"variants/{k}/preview.mp4"))
            listed.append({"label": var["label"], "video": has_video, "code": var["code"]})
        self._stage_media(files)
        if listed:
            pending = self.media_root / "pending"
            for k, var in enumerate(listed, 1):
                (pending / "variants" / str(k)).mkdir(parents=True, exist_ok=True)
                (pending / "variants" / str(k) / "sketch.pde").write_text(var.pop("code"))
            (pending / VARIANTS_FILE).write_text(json.dumps(listed))
        return self._commit(message)

    def copy_version_from(self, source: "Project", sha: str) -> None:
        """Replace this project's folder with `source` at version `sha`, media included."""
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        for path in git("ls-tree", "-r", "--name-only", f"{sha}:{source.rel}").split("\n"):
            if path and path != "README.md" and not path.startswith("frames/") and path not in MEDIA_FILES:
                dest = self.dir / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(source.show(sha, path))
        tmp = self.media_root / "tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        files = []
        for name in MEDIA_FILES:
            try:
                (tmp / name).write_bytes(source.media_file(sha, name))
                files.append((tmp / name, name))
            except RuntimeError:  # this version has no video
                pass
        self._stage_media(files)
        shutil.rmtree(tmp, ignore_errors=True)

    def _stage_media(self, files: Iterable[tuple[Path, str]]) -> None:
        """Copy (source, name) pairs into media/pending/, which _commit renames to the new
        version's sha, and refresh the committed thumbnail from preview.png."""
        pending = self.media_root / "pending"
        shutil.rmtree(pending, ignore_errors=True)
        pending.mkdir(parents=True)
        for src, name in files:
            (pending / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, pending / name)
        thumb = self.dir / THUMB_FILE
        if (pending / "preview.png").exists():
            make_thumb(pending / "preview.png", thumb)
        elif thumb.exists():
            thumb.unlink()

    def _commit(self, message: str) -> dict:
        n = max((v["n"] for v in self.versions()), default=0) + 1
        # Media from older versions may still be in the folder; it now lives outside git.
        shutil.rmtree(self.dir / "frames", ignore_errors=True)
        for name in MEDIA_FILES:
            (self.dir / name).unlink(missing_ok=True)
        self._write_readme(n, message)
        # Commit only this project's folder; anything else staged in the repo is left alone.
        git("add", "-A", "--", self.rel)
        subject = f"[{self.slug}] v{n}: {' '.join(message.split())[:60]}"
        git("commit", "-q", "-m", subject, "-m", message, "--", self.rel)
        git("tag", f"{self.slug}/v{n}")
        sha = git("rev-parse", "HEAD").strip()
        pending = self.media_root / "pending"
        if pending.exists():
            pending.rename(self.media_root / sha)
        return {"n": n, "sha": sha, "message": message}

    def _write_readme(self, n: int, message: str) -> None:
        lines = [f"# {self.name}", "",
                 "A [Processing](https://processing.org/) sketch generated with Processing Agent.", "",
                 ]
        if (self.dir / THUMB_FILE).exists():
            lines += [f"![preview]({THUMB_FILE})", ""]
        lines += [f"**Latest version:** v{n}: {message}", "",
                  "Open `sketch/sketch.pde` in Processing to run it. Full-size frames and video "
                  "are kept locally by Processing Agent, not in git.", ""]
        (self.dir / "README.md").write_text("\n".join(lines))

    # --- persistence -----------------------------------------------------

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "name": self.name, "cc_session_id": self.session.cc_session_id,
            "needs_context": self.needs_context, "events": self.events,
            "tool_version": self.session.tool_version,
        }))

    def summary(self) -> dict:
        versions = self.versions()
        return {
            "slug": self.slug, "name": self.name, "busy": self.busy,
            "versions": len(versions),
            "updated": versions[0]["date"] if versions else None,
            "thumb": f"/api/projects/{self.slug}/versions/{versions[0]['sha']}/thumb.jpg" if versions else None,
        }

    def prune_runs(self) -> None:
        """Delete this project's run folders (each attempt's sketch, frames and video)."""
        for d in self.session.dir.glob("run-*"):
            shutil.rmtree(d, ignore_errors=True)

    async def emit(self, event: dict) -> None:
        self.events.append(event)
        async with self.changed:
            self.changed.notify_all()


class ProjectStore:
    def __init__(self, root: Path, runs_dir: Path) -> None:
        self.root, self.runs_dir = root, runs_dir
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():  # first run: start an empty sketches repo
            git("init", "-q", "-b", "main")
        self.projects: dict[str, Project] = {}
        self.sync = {"state": "idle", "at": None, "error": None, "url": None}
        self._push_task: asyncio.Task | None = None
        self._push_again = False
        self.publisher = None  # set by the server: a gallery.Publisher, run after each new version
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                self._load(d)

    def _load(self, d: Path) -> Project:
        p = Project(slug=d.name, dir=d, session=Session(id=d.name, dir=self.runs_dir / d.name))
        state = json.loads(p.state_path.read_text()) if p.state_path.exists() else {}
        p.name = state.get("name") or d.name
        p.events = state.get("events", [])
        # A request cut off by a server restart never emitted "idle"; close it out.
        last_user = max((i for i, e in enumerate(p.events) if e.get("type") == "user"), default=None)
        if last_user is not None and not any(e.get("type") == "idle" for e in p.events[last_user:]):
            p.events += [{"type": "error", "text": "This request was interrupted (the server restarted)."},
                         {"type": "idle"}]
        p.session.cc_session_id = state.get("cc_session_id")
        p.session.tool_version = state.get("tool_version", 1)  # chats saved before versioning
        # A restart between writing a version's files and committing them leaves them
        # uncommitted; finish that commit.
        if (p.dir / SKETCH_FILE).exists() and git("status", "--porcelain", "--", p.rel, check=False).strip():
            prompt = next((e["text"] for e in reversed(p.events) if e.get("type") == "user"), "Recovered changes")
            v = p._commit(prompt)
            idle = max((i for i, e in enumerate(p.events) if e.get("type") == "idle"), default=len(p.events))
            p.events.insert(idle, {"type": "version", **v})
            p.save_state()
        p.session.latest_code = p.current_code()
        # The API backend keeps history in memory only, so a restart loses it; Claude Code
        # sessions resume, but we can't know which backend the next turn uses.
        p.needs_context = state.get("needs_context", False) or (
            p.session.latest_code is not None and not p.session.cc_session_id)
        self.projects[p.slug] = p
        return p

    def get(self, slug: str) -> Project:
        if slug not in self.projects:
            raise KeyError(slug)
        return self.projects[slug]

    def create(self, name: str, from_project: Project | None = None, sha: str | None = None) -> Project:
        base = slugify(name)
        slug, i = base, 2
        while (self.root / slug).exists():
            slug, i = f"{base}-{i}", i + 1
        d = self.root / slug
        d.mkdir()
        p = self._load(d)
        p.name = name.strip() or slug
        if from_project and sha:
            v = next(v for v in from_project.versions() if v["sha"] == sha)
            p.copy_version_from(from_project, sha)
            p._commit(f"Forked from {from_project.name} v{v['n']}")
            p.session.latest_code = p.current_code()
            p.needs_context = True
            self.auto_push()
        p.save_state()
        return p

    def reset_chat(self, p: Project) -> None:
        p.events.clear()
        p.prune_runs()
        p.session.run_count = 0
        p.session.messages.clear()
        p.session.cc_session_id = None
        p.session.tool_version = TOOL_VERSION
        p.needs_context = p.session.latest_code is not None
        p.save_state()

    async def restore(self, p: Project, sha: str) -> dict:
        v = next((v for v in p.versions() if v["sha"] == sha), None)
        if not v:
            raise KeyError(sha)
        p.copy_version_from(p, sha)
        new = p._commit(f"Restore v{v['n']}")
        p.session.latest_code = p.current_code()
        p.needs_context = True
        await p.emit({"type": "version", **new})
        self.auto_push()
        p.save_state()
        return new

    def auto_push(self) -> None:
        if AUTO_PUSH:
            self.request_push()

    def request_push(self) -> None:
        """Push in the background. Requests made while a push is running are merged into
        one follow-up push, so commits from several projects don't race each other."""
        if self._push_task and not self._push_task.done():
            self._push_again = True
            return
        self._push_task = asyncio.create_task(self._push_loop())

    async def _push_loop(self) -> None:
        while True:
            self._push_again = False
            self.sync["state"] = "pushing"
            for attempt in range(3):  # GitHub sometimes rejects a push with a one-off server error
                try:
                    self.sync["url"] = await asyncio.to_thread(self.push)
                    self.sync.update(state="ok", error=None)
                    break
                except Exception as e:  # e.g. offline; the next version retries
                    if attempt == 2:
                        self.sync.update(state="error", error=str(e)[:300])
                    else:
                        await asyncio.sleep(5 * (attempt + 1))
            self.sync["at"] = time.time()
            if not self._push_again:
                return

    def push(self) -> str:
        """Push the sketches repo (all projects) to GitHub, creating one private repo the
        first time. Returns the repo URL."""
        if not shutil.which("gh"):
            raise RuntimeError("GitHub CLI (gh) isn't installed.")
        if not git("rev-parse", "--verify", "HEAD", check=False).strip():
            raise RuntimeError("Nothing to push yet: make a version first.")
        if not git("remote", "get-url", "origin", check=False).strip():
            out = subprocess.run(
                ["gh", "repo", "create", GITHUB_REPO_NAME, "--private", "--source", ".",
                 "--remote", "origin", "--description", "My Processing Agent sketches"],
                cwd=SKETCHES_DIR, capture_output=True, text=True,
            )
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip() or "gh repo create failed")
        branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
        git("push", "-q", "-u", "origin", branch)
        git("push", "-q", "origin", "--tags")
        url = git("remote", "get-url", "origin").strip()
        return re.sub(r"\.git$", "", url)

    async def run_prompt(self, p: Project, prompt: str, agent) -> None:
        """Run one agent turn in the background; commit a version if it produced new code."""
        await p.emit({"type": "user", "text": prompt, "t": time.time()})
        p.save_state()  # so a restart mid-request still knows what was asked
        sent = prompt
        if p.needs_context and p.session.latest_code:
            sent = (f"(Context: the project's current sketch is below. Start from it.)\n\n"
                    f"```processing\n{p.session.latest_code}\n```\n\n{prompt}")
        if (p.session.cc_session_id or p.session.messages) and p.session.tool_version < TOOL_VERSION:
            sent = (f"(Note from the app: run_sketch has changed since earlier in this conversation. "
                    f"{TOOL_CHANGES} Go by this, not by limits mentioned earlier.)\n\n{sent}")
        p.session.tool_version = TOOL_VERSION
        before = p.session.latest_code
        last_ok: dict | None = None
        turn: list[dict] = []
        try:
            async for event in agent.run_turn(p.session, sent):
                if event["type"] == "result" and event["ok"]:
                    last_ok = event
                if event["type"] in ("running", "result"):
                    turn.append(event)
                await p.emit(event)
            p.needs_context = False
        except Exception as e:  # surface anything unexpected in the UI
            await p.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
        if last_ok and p.session.latest_code and p.session.latest_code != before:
            run_dir = p.session.dir / last_ok["run"]
            stills = sorted((run_dir / "sketch" / "__frames").glob("*.png"))
            video = run_dir / "video.mp4"
            variants = [{"label": r.get("variant"), "code": r["code"],
                         "stills": sorted((p.session.dir / r["run"] / "sketch" / "__frames").glob("*.png")),
                         "video": p.session.dir / r["run"] / "video.mp4"} for r in pick_variants(turn)]
            v = p.commit_version(p.session.latest_code, prompt, stills,
                                 video if video.exists() else None, variants)
            await p.emit({"type": "version", **v})
            self.auto_push()
            if self.publisher:
                self.publisher.request()
        await p.emit({"type": "idle"})
        p.save_state()

    def start(self, p: Project, prompt: str, agent) -> None:
        p.task = asyncio.create_task(self.run_prompt(p, prompt, agent))


def pick_variants(turn: list[dict]) -> list[dict]:
    """The ok results of one request that are alternatives for the user to compare, in the
    order they were started; [] for an ordinary request.

    Runs Claude labeled with `variant` count, the latest result per label. Without labels, a
    final batch of two or more runs started together (all `running` before any `result`, as
    when Claude renders options in parallel) counts too: otherwise only the last one would be
    kept, and the rest of what the user asked for would be lost."""
    order = [e["run"] for e in turn if e["type"] == "running"]
    ok = {e["run"]: e for e in turn if e["type"] == "result" and e["ok"]}
    by_label: dict[str, dict] = {}
    for run in order:
        if run in ok and ok[run].get("variant"):
            by_label.pop(ok[run]["variant"], None)  # a re-run of a label replaces it, at its new place
            by_label[ok[run]["variant"]] = ok[run]
    if len(by_label) >= 2:
        return list(by_label.values())
    batch: list[str] = []
    for e in turn:
        if e["type"] == "running":
            if batch and batch[-1] is None:  # a result came in since this batch started
                batch = []
            batch.append(e["run"])
        elif batch and batch[-1] is not None:
            batch.append(None)
    found = [ok[r] for r in batch if r in ok]
    return found if len(found) >= 2 else []


def make_thumb(src: Path, dest: Path, width: int = 480) -> None:
    """Write a small JPEG of `src` for git and the GitHub README."""
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src),
         "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", "-frames:v", "1", str(dest)],
        check=True, capture_output=True,
    )
