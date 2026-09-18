"""
Test bootstrap.

Six scripts call path_config.resolve() at import time. With no Canvas token
that is network-free, but it still scans for a .env file and writes
canvas_config.json next to the scripts. Everything below runs before the
first script import so the tests never read the developer's real .env, never
touch Canvas, and never leave a config file in the repo.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT    = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

_TMP = Path(tempfile.mkdtemp(prefix="hbs-course-helper-tests-"))
(_TMP / "coursework").mkdir()

os.environ["COURSEWORK_ROOT"]    = str(_TMP / "coursework")
os.environ["CANVAS_CONFIG_FILE"] = str(_TMP / "canvas_config.json")
for _key in ("CANVAS_API_TOKEN", "CANVAS_BASE_URL", "CANVAS_API_URL",
             "ANTHROPIC_API_KEY", "COURSEWORK_TRASH", "CALENDAR_BACKEND"):
    os.environ.pop(_key, None)

import path_config  # noqa: E402  (must follow the environment setup above)

# Belt and braces for versions of path_config that predate CANVAS_CONFIG_FILE.
path_config.CONFIG_FILE = _TMP / "canvas_config.json"
path_config._find_env_file_real = path_config._find_env_file   # for its own test
path_config._find_env_file = lambda *a, **k: None


@pytest.fixture
def fake_paths(tmp_path):
    """A resolve()-shaped dict with one course, LTV, rooted in tmp_path."""
    course_dir = tmp_path / "Fall" / "LTV"
    course_dir.mkdir(parents=True)
    return {
        "canvas_base":     "https://example.test/api/v1",
        "env_file":        None,
        "coursework_root": tmp_path,
        "prompts_dir":     ROOT / "prompts",
        "master_prompt":   ROOT / "prompts" / "cheat_sheet_prompt.md",
        "courses": {
            "LTV": {
                "canvas_id":         1,
                "full_name":         "Launching Tech Ventures",
                "folder_name":       "Fall/LTV",
                "folder_path":       course_dir,
                "refinement_prompt": None,
                "term":              "Fall",
                "term_end":          None,
            },
        },
    }


@pytest.fixture
def patch_courses(monkeypatch, fake_paths):
    """Point every module-level course table at fake_paths."""
    import canvas_refresh as cr

    monkeypatch.setattr(path_config, "resolve", lambda *a, **k: fake_paths)
    monkeypatch.setattr(cr, "_COURSES", fake_paths["courses"])
    ids = {a: d["canvas_id"] for a, d in fake_paths["courses"].items()}
    monkeypatch.setattr(cr, "COURSES", ids)
    monkeypatch.setattr(cr, "ACTIVE_COURSES", dict(ids))
    monkeypatch.setattr(cr, "DEST_ROOT", fake_paths["coursework_root"])
    return fake_paths
