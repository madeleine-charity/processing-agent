import asyncio

import pytest

from app import runner

needs_processing = pytest.mark.skipif(not runner.JAVA_BIN.exists(), reason="Processing isn't installed")


def test_static_mode_is_wrapped_in_setup():
    assert runner.prepare_sketch("size(100, 100);\nbackground(0);").startswith("void setup() {")
    code = "void setup() { size(100, 100); }"
    assert runner.prepare_sketch(code) == code


@needs_processing
def test_renders_a_frame(tmp_path):
    result = asyncio.run(runner.run_sketch(tmp_path / "sketch", "size(120, 80);\nbackground(255, 0, 0);", [1]))
    assert result.ok, result.output
    assert len(result.frames) == 1 and result.frames[0].stat().st_size > 0


@needs_processing
def test_runtime_exception_fails_fast(tmp_path):
    code = "void setup() { size(100, 100); }\nvoid draw() { int[] a = new int[1]; a[5] = 1; }"
    result = asyncio.run(runner.run_sketch(tmp_path / "sketch", code, [10]))
    assert not result.ok and not result.timed_out
    assert "ArrayIndexOutOfBoundsException" in result.output


@needs_processing
def test_compile_error_is_reported(tmp_path):
    result = asyncio.run(runner.run_sketch(tmp_path / "sketch", "void setup() { size(100, 100) }", [1]))
    assert not result.ok and result.output


@needs_processing
def test_running_out_of_time_while_recording_keeps_a_shorter_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUN_TIMEOUT_S", 5)
    monkeypatch.setattr(runner, "VIDEO_TIMEOUT_PER_S", 1)
    # About 20 fps, so 6 s of video (360 frames, ~18 s) can't finish within 5 + 6 s, and the
    # 5 s head start leaves room for a slow JVM start before frame 1.
    code = "void setup() { size(100, 100); }\nvoid draw() { background(frameCount % 255); delay(50); }"
    result = asyncio.run(runner.run_sketch(tmp_path / "sketch", code, [1], video_seconds=6))
    assert result.ok and result.video_cut and not result.timed_out
    assert result.video and 0 < result.video_seconds < 6


@needs_processing
def test_a_cut_run_keeps_the_stills_it_reached(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUN_TIMEOUT_S", 5)
    monkeypatch.setattr(runner, "VIDEO_TIMEOUT_PER_S", 1)
    code = "void setup() { size(100, 100); }\nvoid draw() { background(frameCount % 255); delay(50); }"
    result = asyncio.run(runner.run_sketch(tmp_path / "sketch", code, [1, 340], video_seconds=6))
    assert result.ok and result.video_cut and result.video
    assert [p.name for p in result.frames] == ["frame-00001.png"]
    assert "Frame 340 wasn't reached" in result.summary()


@needs_processing
def test_big_frames_are_scaled_down_for_claude(tmp_path):
    result = asyncio.run(runner.run_sketch(tmp_path / "sketch", "size(3600, 2000);\nbackground(30, 90, 160);", [1]))
    assert result.ok, result.output
    view, label = runner.frame_for_model(result.frames[0])
    assert view.suffix == ".jpg" and view.stat().st_size < 1_000_000
    assert "scaled to 1568x" in label  # Claude is told the real size and the scaled one
    assert result.frames[0].stat().st_size > view.stat().st_size  # the user keeps the original
