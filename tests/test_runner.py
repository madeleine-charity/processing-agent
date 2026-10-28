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
    monkeypatch.setattr(runner, "RUN_TIMEOUT_S", 2)
    monkeypatch.setattr(runner, "VIDEO_TIMEOUT_PER_S", 1)
    # About 20 fps, so 4 s of video (240 frames) can't finish within 2 + 4 s.
    code = "void setup() { size(100, 100); }\nvoid draw() { background(frameCount % 255); delay(50); }"
    result = asyncio.run(runner.run_sketch(tmp_path / "sketch", code, [1], video_seconds=4))
    assert result.ok and result.video_cut and not result.timed_out
    assert result.video and 0 < result.video_seconds < 4
