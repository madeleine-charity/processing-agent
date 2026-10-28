"""Compile and run Processing sketches without opening windows, capturing frames.

Sketches are compiled with the Processing CLI, then run in a JVM we launch ourselves:
the injected capture tab swaps Processing's window for its windowless PSurfaceNone, and
`apple.awt.UIElement` keeps the process out of the Dock and from stealing focus.
"""

import asyncio
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg

PROCESSING_APP = Path(os.environ.get("PROCESSING_APP", "/Applications/Processing.app"))
PROCESSING_BIN = os.environ.get("PROCESSING_BIN", str(PROCESSING_APP / "Contents/MacOS/Processing"))
RESOURCES = PROCESSING_APP / "Contents/app/resources"
JAVA_BIN = RESOURCES / "jdk/bin/java"
CORE_LIBS = RESOURCES / "core/library"
RUN_TIMEOUT_S = float(os.environ.get("SKETCH_TIMEOUT", "60"))
# Recording is slow for heavy sketches (around 5 saved frames a second), so each second of
# video adds this much to the run's time limit.
VIDEO_TIMEOUT_PER_S = 10

# Injected as a second tab. Renders Java2D sketches without a window and registers a
# `post()` hook that saves the requested still frames (plus every VIDEO_STEP-th frame
# for a video clip) and exits, so sketches don't need to call saveFrame() themselves.
CAPTURE_TAB = """\
// Injected by processing-agent: renders offscreen, captures frames, then exits.
public PSurface initSurface() {{
  // OpenGL renderers (P2D/P3D) need a real window, so only Java2D goes windowless.
  if (!sketchRenderer().equals(JAVA2D)) return super.initSurface();
  g = createPrimaryGraphics();
  surface = new PSurfaceNone(g);
  surface.initOffscreen(this);
  return surface;
}}
public class __Capture {{
  int[] frames = {{{frames}}};
  int idx = 0;
  int videoEnd = {video_end};
  int videoStep = {video_step};
  int vidx = 0;
  public void post() {{
    boolean looping = isLooping();
    if (looping && frameCount <= videoEnd && (frameCount - 1) % videoStep == 0) {{
      saveFrame("__video/v-" + nf(vidx++, 5) + ".png");
    }}
    if (idx < frames.length && (frameCount >= frames[idx] || !looping)) {{
      saveFrame("__frames/frame-" + nf(frameCount, 5) + ".png");
      idx++;
    }}
    if (!looping || (idx >= frames.length && frameCount >= videoEnd)) exit();
  }}
}}
__Capture __cap = __installCapture();
__Capture __installCapture() {{ __Capture c = new __Capture(); registerMethod("post", c); return c; }}
"""

# Sketches default to 60 fps; recording every 2nd frame and encoding at 30 fps
# plays back at the sketch's intended speed.
VIDEO_STEP = 2
VIDEO_FPS = 30
MAX_VIDEO_SECONDS = 60

NOISE = ("pixelDensity(2) by default", "Finished.")
# Frames shown to Claude are scaled to fit this long edge (what the API looks at anyway) and
# sent as JPEG: full-size frames of big canvases are 5-15 MB PNGs, over the API's image limit,
# and Claude would see no image at all. The user still gets the full-size files.
VIEW_LONG_EDGE = 1568
JAVA_FRAME = re.compile(r"\(sketch\.java:(\d+)\)")


@dataclass
class RunResult:
    ok: bool
    output: str
    frames: list[Path] = field(default_factory=list)
    timed_out: bool = False
    video: Path | None = None
    video_seconds: float = 0.0
    video_cut: bool = False  # hit the time limit after the stills, so the clip is shorter

    def summary(self) -> str:
        """Text the model sees alongside the frames."""
        text = "Run succeeded." if self.ok else "Run failed."
        if self.video_cut:
            text += " The time limit was reached while recording, so the video is shorter than requested."
        elif self.timed_out:
            text += " Timed out before all frames were captured (sketch too slow or frames too far out)."
        if not self.frames and not self.timed_out:
            text += " No frames were captured."
        if self.video:
            text += f" Recorded a {self.video_seconds:.1f}s video clip for the user."
        return text + (f"\n\nConsole output:\n{self.output}" if self.output else "")


def _is_static_mode(code: str) -> bool:
    """Static-mode sketches have no function definitions at all."""
    return not re.search(r"\bvoid\s+\w+\s*\(", code)


def prepare_sketch(code: str) -> str:
    # Static-mode code can't coexist with the injected tab, so wrap it in setup().
    if _is_static_mode(code):
        body = "\n".join("  " + line for line in code.splitlines())
        return f"void setup() {{\n{body}\n}}\n"
    return code


def write_sketch(sketch_dir: Path, code: str) -> Path:
    sketch_dir.mkdir(parents=True, exist_ok=True)
    pde = sketch_dir / f"{sketch_dir.name}.pde"
    pde.write_text(code)
    return pde


async def run_sketch(
    sketch_dir: Path, code: str, capture_frames: list[int], video_seconds: float = 0
) -> RunResult:
    """Write `code` into `sketch_dir`, run it, and return captured frames, an optional
    video clip (animated sketches only), and console output."""
    frames = sorted({max(1, int(f)) for f in capture_frames}) or [1]
    video_seconds = max(0.0, min(float(video_seconds or 0), MAX_VIDEO_SECONDS))
    # Without draw() there's only ever one frame, so there's nothing to record.
    has_draw = re.search(r"\bvoid\s+draw\s*\(", code)
    video_end = int(video_seconds * VIDEO_FPS * VIDEO_STEP) if has_draw else 0

    if sketch_dir.exists():
        shutil.rmtree(sketch_dir)
    pde = write_sketch(sketch_dir, prepare_sketch(code))
    (sketch_dir / "zz_capture.pde").write_text(
        CAPTURE_TAB.format(
            frames=", ".join(map(str, frames)), video_end=video_end, video_step=VIDEO_STEP
        )
    )
    build_dir = sketch_dir.parent / f"{sketch_dir.name}-build"

    # 1. Compile. Errors come back as `sketch.pde:line:col:...: message`.
    rc, lines, timed_out = await _run_and_collect(
        [PROCESSING_BIN, "cli", f"--sketch={sketch_dir}", f"--output={build_dir}", "--force", "--build"],
        stop_on_error=False,
    )
    if rc != 0 or timed_out:
        return RunResult(ok=False, output="\n".join(lines).strip(), timed_out=timed_out)

    # 2. Run the compiled sketch ourselves, windowless.
    rc, lines, timed_out = await _run_and_collect(
        [str(JAVA_BIN), "-Dapple.awt.UIElement=true", "-Djna.nosys=true",
         f"-Djava.library.path={CORE_LIBS}",
         "-cp", f"{build_dir}:{CORE_LIBS}/*",
         "processing.core.PApplet", f"--sketch-path={sketch_dir}", sketch_dir.name],
        stop_on_error=True,
        timeout=RUN_TIMEOUT_S + video_seconds * VIDEO_TIMEOUT_PER_S,
    )
    output = _map_java_lines("\n".join(lines).strip(), build_dir, pde)
    captured = sorted((sketch_dir / "__frames").glob("*.png"))
    # Out of time while recording, with every still captured: keep the run and a shorter clip.
    video_cut = timed_out and video_end > 0 and len(captured) == len(frames) and "Exception" not in output
    ok = bool(captured) and "Exception" not in output and ((rc == 0 and not timed_out) or video_cut)
    video, seconds = await _encode_video(sketch_dir) if ok else (None, 0.0)
    return RunResult(ok=ok, output=output, frames=captured, timed_out=timed_out and not video_cut,
                     video=video, video_seconds=seconds, video_cut=video_cut)


def frame_for_model(frame: Path) -> tuple[Path, str]:
    """A copy of `frame` small enough to send to Claude, and a label saying how it was scaled."""
    data = frame.read_bytes()[:24]
    w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    label = f"Frame {int(frame.stem.split('-')[1])} ({w}x{h}"
    view = frame.parent / "view" / f"{frame.stem}.jpg"
    view.parent.mkdir(exist_ok=True)
    scale = min(1.0, VIEW_LONG_EDGE / max(w, h, 1))
    vw, vh = max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(frame),
                    "-vf", f"scale={vw}:{vh}", "-q:v", "3", "-pix_fmt", "yuvj420p", str(view)],
                   check=True, capture_output=True)
    if scale < 1:
        label += f", shown to you scaled to {vw}x{vh}"
    return view, label + "):"


async def _run_and_collect(cmd: list[str], stop_on_error: bool,
                           timeout: float = RUN_TIMEOUT_S) -> tuple[int | None, list[str], bool]:
    """Run `cmd` in its own process group, collecting output. With `stop_on_error`, kill it
    as soon as an exception is printed (sketches can keep running after one)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    lines: list[str] = []
    errored = False

    async def read_output() -> None:
        nonlocal errored
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if any(n in line for n in NOISE):
                continue
            lines.append(line)
            if stop_on_error and "Exception" in line and not errored:
                errored = True
                # Let the rest of the stack trace arrive, then stop.
                try:
                    await asyncio.wait_for(_drain(proc, lines), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                return

    timed_out = False
    try:
        await asyncio.wait_for(read_output(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
    _kill_group(proc)
    await proc.wait()
    return (None if errored else proc.returncode), lines, timed_out


async def _drain(proc: asyncio.subprocess.Process, lines: list[str]) -> None:
    assert proc.stdout
    async for raw in proc.stdout:
        lines.append(raw.decode(errors="replace").rstrip())


def _map_java_lines(output: str, build_dir: Path, pde: Path) -> str:
    """Stack traces point at the generated sketch.java; add the matching sketch.pde line."""
    java = build_dir / "source" / "sketch.java"
    if not JAVA_FRAME.search(output) or not java.exists():
        return output
    java_lines = java.read_text().splitlines()
    pde_lines = [l.strip() for l in pde.read_text().splitlines()]

    def annotate(m: re.Match) -> str:
        n = int(m.group(1))
        src = java_lines[n - 1].strip() if 0 < n <= len(java_lines) else ""
        if src and src in pde_lines:
            return f"(sketch.pde:{pde_lines.index(src) + 1}: `{src}`)"
        return m.group(0)

    return JAVA_FRAME.sub(annotate, output)


async def _encode_video(sketch_dir: Path) -> tuple[Path | None, float]:
    """Encode __video/*.png into video.mp4 next to the sketch; returns (path, seconds)."""
    src = sketch_dir / "__video"
    count = len(list(src.glob("v-*.png")))
    if count < 2:  # static sketch (noLoop) or video not requested
        shutil.rmtree(src, ignore_errors=True)
        return None, 0.0
    out = sketch_dir.parent / "video.mp4"
    proc = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
        "-framerate", str(VIDEO_FPS), "-i", str(src / "v-%05d.png"),
        # Retina runs save frames at 2x density, so halve large frames; H.264 needs even dimensions.
        "-vf", r"scale=trunc(if(gt(iw\,1400)\,iw/2\,iw)/2)*2:-2",
        # Noisy, detailed sketches compress badly: a 40 s clip was 182 MB at crf 20, 91 MB at 26.
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-movflags", "+faststart",
        str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    shutil.rmtree(src, ignore_errors=True)
    if proc.returncode != 0 or not out.exists():
        return None, 0.0
    return out, count / VIDEO_FPS


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def launch_live(sketch_dir: Path, code: str) -> None:
    """Open the sketch in a normal window on this machine (no capture, no timeout)."""
    live_dir = sketch_dir.parent / "live"
    if live_dir.exists():
        shutil.rmtree(live_dir)
    write_sketch(live_dir, code)
    await asyncio.create_subprocess_exec(
        PROCESSING_BIN, "cli",
        f"--sketch={live_dir}", f"--output={live_dir.parent / 'live-build'}", "--force", "--run",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
