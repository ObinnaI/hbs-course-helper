"""git_resolve: a stopped rebase is finished without a human — state JSON union-merged, other files kept."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import git_resolve as gr   # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "git_resolve.py"


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@x", GIT_EDITOR="true", HOME=str(cwd))
    return subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=check,
                          capture_output=True, text=True, env=env)


def test_union_keeps_both_sides_and_prefers_b_on_clashes():
    a = {"fetched": {"x": {"at": 1}}, "261001-aa": {"filed": "A"}, "n": 1}
    b = {"fetched": {"y": {"at": 2}}, "261001-aa": {"filed": "B"}, "m": 2}
    assert gr.union(a, b) == {"fetched": {"x": {"at": 1}, "y": {"at": 2}},
                              "261001-aa": {"filed": "B"}, "n": 1, "m": 2}
    assert gr.union([1], [2]) == [2] and gr.union(1, {"a": 1}) == {"a": 1}


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"; origin.mkdir()
    git(origin, "init", "--bare", "-q")
    mac = tmp_path / "mac"; cloud = tmp_path / "cloud"
    git(tmp_path, "clone", "-q", str(origin), str(mac))
    (mac / "claude").mkdir()
    (mac / "claude" / "inbox_state.json").write_text(json.dumps({"fetched": {"base": {}}}, indent=2, sort_keys=True) + "\n")
    (mac / "notes.md").write_text("base\n")
    git(mac, "add", "-A"); git(mac, "commit", "-qm", "base"); git(mac, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(cloud))
    return mac, cloud


def _write_state(repo, extra):
    p = repo / "claude" / "inbox_state.json"
    d = json.loads(p.read_text()); d["fetched"].update(extra); p.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")


def test_conflicting_push_is_resolved_in_favour_of_the_replayed_commit(repos):
    mac, cloud = repos
    # The Mac pushes a one-pager, a state entry and a note edit while the cloud run is working.
    _write_state(mac, {"mac-id": {"subject": "seeded"}})
    (mac / "Offer One-Pager - X.docx").write_bytes(b"MAC")
    (mac / "notes.md").write_text("mac\n")
    git(mac, "add", "-A"); git(mac, "commit", "-qm", "mirror"); git(mac, "push", "-q", "origin", "main")
    # The cloud run touches the same three paths.
    _write_state(cloud, {"cloud-id": {"subject": "fetched"}})
    (cloud / "Offer One-Pager - X.docx").write_bytes(b"CLOUD")
    (cloud / "notes.md").write_text("cloud\n")
    git(cloud, "add", "-A"); git(cloud, "commit", "-qm", "refresh")
    r = git(cloud, "pull", "--rebase", "-q", "origin", "main", check=False)
    assert r.returncode != 0 and (cloud / ".git" / "rebase-merge").exists()   # it stopped on conflicts

    res = subprocess.run([sys.executable, str(SCRIPT)], cwd=cloud, capture_output=True, text=True,
                         env=dict(os.environ, HOME=str(cloud), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                                  GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x"))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "union-merged" in res.stdout and "replayed commit" in res.stdout
    state = json.loads((cloud / "claude" / "inbox_state.json").read_text())
    assert set(state["fetched"]) == {"base", "mac-id", "cloud-id"}         # nobody's entry is lost
    assert (cloud / "Offer One-Pager - X.docx").read_bytes() == b"CLOUD"   # the run's generated file wins
    assert (cloud / "notes.md").read_text() == "cloud\n"
    assert not (cloud / ".git" / "rebase-merge").exists()
    git(cloud, "push", "-q", "origin", "main")                              # and the push now goes through
    assert "refresh" in git(cloud, "log", "--oneline", "-1").stdout


def test_no_rebase_in_progress_exits_2(repos):
    _mac, cloud = repos
    res = subprocess.run([sys.executable, str(SCRIPT)], cwd=cloud, capture_output=True, text=True)
    assert res.returncode == 2 and "nothing to resolve" in res.stdout
