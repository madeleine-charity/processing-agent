"""Publish versions to the Processing Gallery: a static Vercel site (../processing-gallery)
that shows each prompt with the image or video Claude made and Claude's reply.

The site's code lives in its own git repo. What this module exports into its content/
folder (gallery.json plus web-sized images and videos) is gitignored there and uploaded
straight to Vercel by `vercel deploy --prod`, so heavy files never go into git.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import imageio_ffmpeg

from .projects import REPO_ROOT, SKETCHES_DIR, Project, git

GALLERY_DIR = Path(os.environ.get("GALLERY_DIR", REPO_ROOT.parent / "processing-gallery"))
# Publish to the gallery after every new version (AUTO_PUBLISH=0 turns publishing off).
AUTO_PUBLISH = os.environ.get("AUTO_PUBLISH", "1") != "0"
DISPLAY_WIDTH = 1600
# Zoom images; Retina renders can be 4800px wide, which is far more than a screen needs.
FULL_WIDTH = 3200
DEPLOY_TIMEOUT_S = 15 * 60
# Restores and forks are bookkeeping, not prompts.
NOT_A_PROMPT = re.compile(r"^(Restore v\d+|Forked from )")


def save_replies(p: Project) -> None:
    """Record each version's prompt and Claude's reply next to its local media, so they
    survive Clear chat. The reply is Claude's last message before the version was saved."""
    user_i = None
    for i, e in enumerate(p.events):
        if e.get("type") == "user":
            user_i = i
        elif e.get("type") == "version" and user_i is not None:
            meta = p.media_root / e["sha"] / "meta.json"
            if meta.exists():
                continue
            texts = [x["text"] for x in p.events[user_i:i] if x.get("type") == "text"]
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps({"prompt": p.events[user_i]["text"],
                                        "reply": texts[-1] if texts else None}))


def _meta(p: Project, sha: str) -> dict:
    path = p.media_root / sha / "meta.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _png_width(data: bytes) -> int | None:
    return int.from_bytes(data[16:20], "big") if data[:8] == b"\x89PNG\r\n\x1a\n" else None


def _jpeg(src: Path, dest: Path, width: int | None, quality: int) -> None:
    scale = ["-vf", f"scale='min({width},iw)':-2"] if width else []
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src), *scale,
                    "-q:v", str(quality), "-pix_fmt", "yuvj420p", "-frames:v", "1", str(dest)], check=True, capture_output=True)


def _web_video(src: bytes, dest: Path) -> None:
    """Re-encode a clip for the web: long, detailed loops can otherwise be 100+ MB."""
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        tmp.write(src)
        tmp.flush()
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", tmp.name,
                        "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                        str(dest)], check=True, capture_output=True)


def _export_version(p: Project, v: dict, out: Path) -> dict:
    """Write a version's web images and video into out/<slug>/ (named by sha, so files
    already exported are reused) and return its gallery entry."""
    meta = _meta(p, v["sha"])
    # Commit subjects are cut at 60 characters; the body holds the whole prompt.
    prompt = meta.get("prompt") or git("log", "-1", "--format=%b", v["sha"]).strip() or v["message"]
    entry = {"n": v["n"], "date": v["date"], "prompt": prompt,
             "reply": meta.get("reply"), "image": None, "full": None, "video": None}
    d = out / p.slug
    d.mkdir(parents=True, exist_ok=True)
    key = v["sha"][:10]
    image, full, video = d / f"{key}.jpg", d / f"{key}-full.jpg", d / f"{key}.mp4"
    try:
        png = p.media_file(v["sha"], "preview.png")
    except (RuntimeError, OSError):
        png = None
    if png:
        width = _png_width(png)
        if not image.exists() or (width and width > DISPLAY_WIDTH and not full.exists()):
            with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
                tmp.write(png)
                tmp.flush()
                _jpeg(Path(tmp.name), image, DISPLAY_WIDTH, 4)
                if width and width > DISPLAY_WIDTH:
                    _jpeg(Path(tmp.name), full, FULL_WIDTH, 4)
        entry["image"] = f"content/{p.slug}/{image.name}"
        if full.exists():
            entry["full"] = f"content/{p.slug}/{full.name}"
    if v["video"]:
        if not video.exists():
            try:
                _web_video(p.media_file(v["sha"], "preview.mp4"), video)
            except (RuntimeError, OSError):
                pass
        if video.exists():
            entry["video"] = f"content/{p.slug}/{video.name}"
    return entry


def export(projects: list[Project], gallery_dir: Path = GALLERY_DIR) -> dict:
    """Rebuild gallery_dir/content/ from every project's prompt versions."""
    out = gallery_dir / "content"
    out.mkdir(parents=True, exist_ok=True)
    listed = []
    for p in projects:
        save_replies(p)
        versions = [v for v in reversed(p.versions()) if not NOT_A_PROMPT.match(v["message"])]
        entries = [e for e in (_export_version(p, v, out) for v in versions) if e["image"] or e["video"]]
        if entries:
            listed.append({"slug": p.slug, "name": p.name, "updated": entries[-1]["date"],
                           "cover": entries[-1]["image"], "versions": entries})
    listed.sort(key=lambda x: x["updated"], reverse=True)
    data = {"updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "projects": listed}
    (out / "gallery.json").write_text(json.dumps(data, indent=1))
    # Drop files from versions or projects that are no longer listed.
    keep = {out / "gallery.json"} | {gallery_dir / e[k] for x in listed for e in x["versions"]
                                     for k in ("image", "full", "video") if e[k]}
    for f in out.rglob("*"):
        if f.is_file() and f not in keep:
            f.unlink()
    for d in out.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return data


def deploy(gallery_dir: Path = GALLERY_DIR) -> str:
    """Deploy the gallery to Vercel production; returns the site URL."""
    if not shutil.which("vercel"):
        raise RuntimeError("Vercel CLI isn't installed.")
    out = subprocess.run(["vercel", "deploy", "--prod", "--yes"], cwd=gallery_dir,
                         capture_output=True, text=True, timeout=DEPLOY_TIMEOUT_S)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[-400:] or "vercel deploy failed")
    aliased = re.search(r"Aliased:?\s+(https://\S+)", out.stderr + out.stdout)
    urls = re.findall(r"https://\S+\.vercel\.app", out.stdout)
    return aliased.group(1) if aliased else (urls[-1] if urls else "")


class Publisher:
    """Exports and deploys in the background. Requests made while one is running are
    merged into one follow-up run, like ProjectStore's pushes."""

    def __init__(self, projects: dict[str, Project], gallery_dir: Path = GALLERY_DIR) -> None:
        self.projects, self.gallery_dir = projects, gallery_dir
        self.status = {"state": "idle", "at": None, "error": None, "url": None}
        self._task: asyncio.Task | None = None
        self._again = False

    def request(self) -> None:
        if self._task and not self._task.done():
            self._again = True
            return
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            self._again = False
            self.status["state"] = "publishing"
            try:
                await asyncio.to_thread(export, list(self.projects.values()), self.gallery_dir)
                self.status["url"] = await asyncio.to_thread(deploy, self.gallery_dir) or self.status["url"]
                self.status.update(state="ok", error=None)
            except Exception as e:  # e.g. offline; the next version retries
                self.status.update(state="error", error=str(e)[:300])
            self.status["at"] = time.time()
            if not self._again:
                return


def _read_projects(root: Path) -> list[Project]:
    """Load projects read-only (unlike ProjectStore, never commits anything), so publishing
    by hand is safe while the server is running a request."""
    from .agent import Session
    result = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            p = Project(slug=d.name, dir=d, session=Session(id=d.name, dir=REPO_ROOT / "runs" / d.name))
            state = json.loads(p.state_path.read_text()) if p.state_path.exists() else {}
            p.name, p.events = state.get("name") or d.name, state.get("events", [])
            result.append(p)
    return result


if __name__ == "__main__":
    data = export(_read_projects(SKETCHES_DIR))
    print(f"Exported {sum(len(x['versions']) for x in data['projects'])} versions; deploying…")
    print(deploy() or "Deployed.")
