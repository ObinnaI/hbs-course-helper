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
    assert pc.default_folder_name("INVS", "Seminar in Investing", "Fall") == "Fall/Seminar in Investing"
    assert pc.default_folder_name("INVS", "Seminar in Investing") == "Seminar in Investing"
    assert pc.default_folder_name("INVS", "INVS", "Fall") == "Fall/INVS"
    assert pc.default_folder_name("INVS", None) == "INVS"
    assert pc.default_folder_name("AI", "AI: Tools/Strategy") == "AI- Tools-Strategy"


def test_term_folder_and_end():
    assert pc.term_folder("Fall 2026") == "Fall"
    assert pc.term_folder("2026 Fall") == "Fall"
    assert pc.term_folder("Autumn Term 2026") == "Fall"
    assert pc.term_folder("Spring 2027") == "Spring"
    assert pc.term_folder("Default Term") is None
    assert pc.term_folder(None) is None
    assert pc.term_end({"term": {"end_at": "2026-12-20T00:00:00Z"}, "end_at": "x"}) == "2026-12-20T00:00:00Z"
    assert pc.term_end({"term": {"end_at": None}, "end_at": "2026-12-01T00:00:00Z"}) == "2026-12-01T00:00:00Z"
    assert pc.term_end({}) is None


def test_is_active_with_grace(monkeypatch):
    from datetime import datetime, timezone
    now = datetime(2027, 1, 15, tzinfo=timezone.utc)
    assert pc.is_active({"term_end": None}, now)
    assert pc.is_active({"term_end": "2026-12-20T00:00:00Z"}, now)       # within 30 days
    assert not pc.is_active({"term_end": "2026-12-01T00:00:00Z"}, now)   # 45 days ago
    assert pc.is_active({"term_end": datetime(2027, 5, 1, tzinfo=timezone.utc)}, now)


def test_find_course_folder_stays_within_term(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "COURSEWORK_ROOT", tmp_path)
    for d in ("Fall/Motivating People", "Fall/negotiations", "Fall/Seminar in Investing",
              "Fall/IP", "Spring/Motivating People", "Overview", "claude"):
        (tmp_path / d).mkdir(parents=True)
    f = pc._find_course_folder
    assert f("MP", None, "Motivating People", "Fall") == "Fall/Motivating People"
    assert f("NEG", None, "Negotiations", "Fall") == "Fall/negotiations"
    assert f("INVS", None, "Seminar in Investing", "Fall") == "Fall/Seminar in Investing"
    assert f("INVS", None, "The Seminar in Investing", "Fall") == "Fall/Seminar in Investing"
    assert f("IP", None, "Intellectual Property Strategy", "Fall") == "Fall/IP"
    assert f("MP", None, "Motivating People", "Spring") == "Spring/Motivating People"
    assert f("MP", None, "Motivating People", "Winter") is None
    assert f("IP", None, "Investing Practicum", "Spring") is None   # no whole-word match
    assert f("MP", "Fall/Motivating People", "Renamed", "Spring") == "Fall/Motivating People"  # cached wins
    assert f("XYZ", None, "", None) is None


def test_discover_keeps_term_for_course_canvas_stopped_returning(monkeypatch):
    cfg = {"courses": {"MP": {"canvas_id": 1, "full_name": "Motivating People",
                              "folder_name": "Fall/Motivating People", "term": "Fall",
                              "term_end": "2026-12-20T00:00:00Z"}},
           "term_folders": {"2": "Spring"}}
    raw = [{"id": 2, "name": "NEG - 00 Negotiations 1234", "course_code": "NEG 27S",
            "term": {"id": 9, "name": "Default Term", "end_at": None}}]
    monkeypatch.setattr(pc, "_fetch_enrolled_courses", lambda token, base: raw)
    merged, changed = pc._discover_courses("t", "https://c.test/api/v1", cfg)
    assert changed
    assert merged["MP"]["term"] == "Fall" and merged["MP"]["term_end"] == "2026-12-20T00:00:00Z"
    assert merged["MP"]["folder_name"] == "Fall/Motivating People"
    assert merged["NEG"]["term"] == "Spring" and merged["NEG"]["term_name"] == "Default Term"


def test_include_term_in_query(monkeypatch):
    seen = {}
    class _Resp:
        headers = {"Link": ""}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"[]"
    def fake_urlopen(req, timeout=15):
        seen["url"] = req.full_url
        return _Resp()
    monkeypatch.setattr(pc, "urlopen", fake_urlopen)
    pc._fetch_enrolled_courses("t", "https://c.test/api/v1")
    assert "include%5B%5D=term" in seen["url"]


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
    assert not (root / "Seminar in Investing").exists()

    paths = pc.resolve()
    assert (root / "Seminar in Investing").is_dir()
    assert paths["courses"]["INVS"]["folder_path"] == root / "Seminar in Investing"
    assert json.loads(conf.read_text())["courses"]["INVS"]["folder_name"] == "Seminar in Investing"


def test_new_course_folder_goes_under_its_term(monkeypatch, tmp_path):
    cfg = {"courses": {"INVS": {"canvas_id": 5, "full_name": "Seminar in Investing",
                                "folder_name": None, "term": "Fall",
                                "term_end": "2026-12-20T00:00:00Z"}},
           "courses_refreshed_at": "2999-01-01T00:00:00+00:00"}
    root, conf = _fresh(monkeypatch, tmp_path, cfg)
    paths = pc.resolve()
    assert (root / "Fall" / "Seminar in Investing").is_dir()
    info = paths["courses"]["INVS"]
    assert info["folder_name"] == "Fall/Seminar in Investing" and info["term"] == "Fall"
    assert info["term_end"].year == 2026


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
    assert pc._find_course_folder("OVERVIEW", None, "Overview", None) is None
