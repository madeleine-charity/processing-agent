import subprocess

import pytest

from app import projects


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway sketches repo, so tests never commit to or push the real one."""
    root = tmp_path / "sketches"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    monkeypatch.setattr(projects, "SKETCHES_DIR", root)
    monkeypatch.setattr(projects, "AUTO_PUSH", False)
    return root


@pytest.fixture
def store(repo):
    return projects.ProjectStore(repo, repo.parent / "runs")
