import json
import os

import path_config as pc


def test_get_canvas_base_normalises():
    assert pc._get_canvas_base({"CANVAS_BASE_URL": "https://c.test/"}) == "https://c.test/api/v1"
    assert pc._get_canvas_base({"CANVAS_API_URL": "https://c.test/api/v1"}) == "https://c.test/api/v1"
    assert pc._get_canvas_base({}) == ""


def test_find_env_file_looks_only_next_to_coursework_or_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "COURSEWORK_ROOT", tmp_path / "cw")
    monkeypatch.setattr(pc, "CLAUDE_DIR", tmp_path / "code")
    (tmp_path / "cw").mkdir(); (tmp_path / "code").mkdir()
    # A .env somewhere else (the old ~/repos/*/.env scan) must not be found.
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / ".env").write_text("CANVAS_API_TOKEN=x\n")
    assert pc._find_env_file_real() is None

    (tmp_path / "cw" / ".env").write_text("CANVAS_API_TOKEN=x\n")
    assert pc._find_env_file_real() == (tmp_path / "cw" / ".env").resolve()


def test_default_folder_name():
    assert pc.default_folder_name("INVS", "Seminar in Investing") == "INVS - Seminar in Investing"
    assert pc.default_folder_name("INVS", "INVS") == "INVS"
    assert pc.default_folder_name("INVS", None) == "INVS"
    assert pc.default_folder_name("AI", "AI: Tools/Strategy") == "AI - AI- Tools-Strategy"


def _fresh(monkeypatch, tmp_path, cfg=None):
    root = tmp_path / "coursework"; root.mkdir()
    conf = tmp_path / "claude" / "canvas_config.json"
    monkeypatch.setattr(pc, "COURSEWORK_ROOT", root)
    monkeypatch.setattr(pc, "CONFIG_FILE", conf)
    if cfg is not None:
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(json.dumps(cfg))
    return root, conf


def test_token_from_environment_drives_discovery(monkeypatch, tmp_path):
    root, conf = _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("CANVAS_API_TOKEN", "tok")
    monkeypatch.setenv("CANVAS_BASE_URL", "https://c.test")
    seen = {}
    monkeypatch.setattr(pc, "_discover_courses",
                        lambda token, base, cfg: seen.update(token=token, base=base) or ({}, False))

    paths = pc.resolve()

    assert seen == {"token": "tok", "base": "https://c.test/api/v1"}
    assert paths["canvas_base"] == "https://c.test/api/v1"
    # No env file was recorded (a runner has none; a Mac would flip it back).
    assert "env_file" not in json.loads(conf.read_text())


def test_new_course_folder_uses_full_name(monkeypatch, tmp_path):
    cfg = {"courses": {"INVS": {"canvas_id": 5, "full_name": "Seminar in Investing",
                                "folder_name": None}},
           "courses_refreshed_at": "2999-01-01T00:00:00+00:00"}
    root, conf = _fresh(monkeypatch, tmp_path, cfg)

    dry = pc.resolve(create_folders=False)
    assert dry["courses"]["INVS"]["folder_path"] is None
    assert not (root / "INVS - Seminar in Investing").exists()

    paths = pc.resolve()
    assert (root / "INVS - Seminar in Investing").is_dir()
    assert paths["courses"]["INVS"]["folder_path"] == root / "INVS - Seminar in Investing"
    assert json.loads(conf.read_text())["courses"]["INVS"]["folder_name"] == "INVS - Seminar in Investing"


def test_seeded_folder_name_wins_when_folder_missing(monkeypatch, tmp_path):
    cfg = {"courses": {"INVS": {"canvas_id": 5, "full_name": "Seminar in Investing",
                                "folder_name": "Investing Seminar"}},
           "courses_refreshed_at": "2999-01-01T00:00:00+00:00"}
    root, conf = _fresh(monkeypatch, tmp_path, cfg)
    paths = pc.resolve()
    assert (root / "Investing Seminar").is_dir()
    assert paths["courses"]["INVS"]["folder_name"] == "Investing Seminar"


def test_overview_folder_is_not_a_course(monkeypatch, tmp_path):
    root, conf = _fresh(monkeypatch, tmp_path)
    (root / "Overview").mkdir()
    assert pc._find_course_folder("OVERVIEW") is None
