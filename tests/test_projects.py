import json
import subprocess

from app import projects


def _png(path, color="red"):
    import imageio_ffmpeg
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color={color}:s=64x48", "-frames:v", "1", str(path)], check=True)
    return path


def _tracked(repo):
    return subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.split()


def test_version_commits_only_its_folder_and_keeps_media_local(store, repo, tmp_path):
    (repo / "unrelated.txt").write_text("leave me alone")
    p = store.create("Red square")
    frame = _png(tmp_path / "frame-00001.png")
    v = p.commit_version("void setup() {}", "a red square", [frame], None)

    assert v["n"] == 1
    tracked = _tracked(repo)
    assert "unrelated.txt" not in tracked
    assert f"{p.slug}/sketch/sketch.pde" in tracked
    assert f"{p.slug}/thumb.jpg" in tracked
    assert not any(f.endswith(("preview.png", ".mp4")) or "/frames/" in f for f in tracked)
    assert p.media_file(v["sha"], "preview.png") == frame.read_bytes()
    assert subprocess.run(["git", "tag"], cwd=repo, capture_output=True, text=True).stdout.split() == [f"{p.slug}/v1"]


def test_restore_and_fork_carry_media(store, tmp_path):
    p = store.create("Colors")
    red = p.commit_version("// red", "red", [_png(tmp_path / "frame-00001.png", "red")], None)
    p.commit_version("// blue", "blue", [_png(tmp_path / "frame-00002.png", "blue")], None)

    import asyncio
    restored = asyncio.run(store.restore(p, red["sha"]))
    assert p.current_code() == "// red"
    assert p.media_file(restored["sha"], "preview.png") == p.media_file(red["sha"], "preview.png")

    fork = store.create("Colors copy", from_project=p, sha=red["sha"])
    assert fork.current_code() == "// red"
    assert fork.media_file(fork.versions()[0]["sha"], "preview.png") == p.media_file(red["sha"], "preview.png")


def test_restart_closes_out_and_commits_an_interrupted_request(store, repo):
    p = store.create("Interrupted")
    p.events.append({"type": "user", "text": "draw a circle"})
    p.save_state()
    # The request wrote its files, then the server died before committing.
    (p.dir / "sketch").mkdir(parents=True)
    (p.dir / projects.SKETCH_FILE).write_text("// circle")

    reloaded = projects.ProjectStore(repo, repo.parent / "runs").get(p.slug)
    assert [v["message"] for v in reloaded.versions()] == ["draw a circle"]
    types = [e["type"] for e in reloaded.events]
    assert types[-3:] == ["error", "version", "idle"]
    assert json.loads(reloaded.state_path.read_text())["events"][-1]["type"] == "idle"


def test_clear_chat_deletes_old_runs(store):
    p = store.create("Runs")
    (p.session.dir / "run-01").mkdir(parents=True)
    store.reset_chat(p)
    assert not list(p.session.dir.glob("run-*"))


def test_fresh_clone_falls_back_to_thumbnail(store, tmp_path):
    import shutil
    p = store.create("Clone")
    v = p.commit_version("// x", "x", [_png(tmp_path / "frame-00001.png")], None)
    shutil.rmtree(p.media_root)  # what a fresh clone looks like
    assert p.media_file(v["sha"], "preview.png") == (p.dir / projects.THUMB_FILE).read_bytes()


def test_gallery_export_skips_restores_and_keeps_replies(store, tmp_path):
    import asyncio
    from app import gallery
    p = store.create("Gallery")
    v1 = p.commit_version("// 1", "first prompt", [_png(tmp_path / "frame-00001.png")], None)
    p.events += [{"type": "user", "text": "first prompt"}, {"type": "text", "text": "thinking aloud"},
                 {"type": "text", "text": "Here is a red square."}, {"type": "version", **v1}, {"type": "idle"}]
    asyncio.run(store.restore(p, v1["sha"]))
    gallery.save_replies(p)
    p.events.clear()  # the reply must survive Clear chat

    data = gallery.export([p], tmp_path / "site")
    [entry] = data["projects"][0]["versions"]
    assert (entry["prompt"], entry["reply"]) == ("first prompt", "Here is a red square.")
    assert (tmp_path / "site" / entry["image"]).exists()


def test_first_run_starts_an_empty_sketches_repo(tmp_path, monkeypatch):
    root = tmp_path / "fresh-sketches"
    monkeypatch.setattr(projects, "SKETCHES_DIR", root)
    store = projects.ProjectStore(root, tmp_path / "runs")
    assert (root / ".git").is_dir() and store.projects == {}


def test_older_chats_hear_about_tool_changes_once(store):
    import asyncio
    from app.agent import TOOL_VERSION

    sent = []

    class FakeAgent:
        async def run_turn(self, session, prompt):
            sent.append(prompt)
            yield {"type": "done"}

    p = store.create("Old chat")
    p.session.cc_session_id = "resumed-session"
    p.session.tool_version = TOOL_VERSION - 1
    asyncio.run(store.run_prompt(p, "longer video please", FakeAgent()))
    asyncio.run(store.run_prompt(p, "and again", FakeAgent()))
    assert "Note from the app" in sent[0] and sent[0].endswith("longer video please")
    assert sent[1] == "and again"


def test_new_chats_get_no_tool_note(store):
    import asyncio

    sent = []

    class FakeAgent:
        async def run_turn(self, session, prompt):
            sent.append(prompt)
            yield {"type": "done"}

    asyncio.run(store.run_prompt(store.create("New chat"), "a red circle", FakeAgent()))
    assert sent == ["a red circle"]


def _fake_run(p, run, tmp_path, video=True):
    d = p.session.dir / run
    (d / "sketch" / "__frames").mkdir(parents=True)
    _png(d / "sketch" / "__frames" / "frame-00001.png")
    if video:
        (d / "video.mp4").write_bytes(b"not really a video")
    return d


def test_parallel_runs_are_all_kept_as_variants(store, tmp_path):
    """Four runs started together (Claude rendering options in parallel) all reach the version."""
    import asyncio
    runs = ["run-01", "run-02", "run-03", "run-04"]

    class FakeAgent:
        async def run_turn(self, session, prompt):
            for r in runs:
                yield {"type": "running", "run": r, "code": f"// {r}"}
            for r in runs:
                session.latest_code = f"// {r}"
                yield {"type": "result", "run": r, "ok": True, "code": f"// {r}"}
            yield {"type": "done"}

    p = store.create("Options")
    for r in runs:
        _fake_run(p, r, tmp_path)
    asyncio.run(store.run_prompt(p, "show me four options", FakeAgent()))
    [v] = p.versions()
    assert [x["video"] for x in v["variants"]] == [True] * 4
    for k, r in enumerate(runs, 1):
        assert p.variant_file(v["sha"], k, "preview.png").exists()
        assert p.variant_file(v["sha"], k, "sketch.pde").read_text() == f"// {r}"


def test_labeled_variants_and_ordinary_retries():
    from app.projects import pick_variants
    ok = lambda r, label=None: {"type": "result", "run": r, "ok": True, "code": "", "variant": label}
    run = lambda r: {"type": "running", "run": r}
    # One at a time, labeled; B is fixed and re-run, and replaces its first try.
    turn = [run("r1"), ok("r1", "A"), run("r2"), {"type": "result", "run": "r2", "ok": False},
            run("r3"), ok("r3", "B"), run("r4"), ok("r4", "B")]
    assert [r["run"] for r in pick_variants(turn)] == ["r1", "r4"]
    # Ordinary attempts in a row are not variants, and neither is one parallel run that worked.
    assert pick_variants([run("r1"), ok("r1"), run("r2"), ok("r2")]) == []
    assert pick_variants([run("r1"), run("r2"), ok("r1"), {"type": "result", "run": "r2", "ok": False}]) == []


def test_gallery_exports_variants(store, tmp_path):
    from app import gallery
    p = store.create("Gallery variants")
    variants = [{"label": label, "code": f"// {label}", "video": None,
                 "stills": [_png(tmp_path / f"{label}.png")]} for label in ("A", "B")]
    v = p.commit_version("// B", "two options", variants[1]["stills"], None, variants)
    p.events += [{"type": "user", "text": "two options"}, {"type": "version", **v}, {"type": "idle"}]
    data = gallery.export([p], tmp_path / "site")
    [entry] = data["projects"][0]["versions"]
    assert [x["label"] for x in entry["variants"]] == ["A", "B"]
    assert all((tmp_path / "site" / x["image"]).exists() for x in entry["variants"])
