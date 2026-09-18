"""
path_config.py — Runtime path resolution for Canvas scripts.

All paths are derived from this file's own location using Path(__file__),
so the entire Coursework folder can be renamed, moved, or copied and the
scripts will still resolve correctly.

Courses are auto-discovered from Canvas API on first run and cached for
24 hours in canvas_config.json — no manual course ID configuration needed.

Required .env keys (environment variables take precedence, so a CI runner
can supply them as secrets with no .env at all):
    CANVAS_API_TOKEN  — Canvas personal access token
    CANVAS_BASE_URL   — Your Canvas domain, e.g. https://yourschool.instructure.com
                        (also accepts CANVAS_API_URL with /api/v1 appended)
    ANTHROPIC_API_KEY — For AI notes generation

Optional:
    COURSEWORK_ROOT    — folder holding the course folders (default: parent of
                         this checkout, i.e. Coursework/ when installed as
                         Coursework/claude/scripts/)
    CANVAS_CONFIG_FILE — where canvas_config.json lives (default: next to this
                         checkout). Point it inside COURSEWORK_ROOT when the
                         coursework is what persists between runs.

Results are cached in canvas_config.json and updated when stale.
If anything has moved or changed, a one-line notice is printed; silent otherwise.

Bootstrap / check what would happen, without creating any folders:
    python3 path_config.py --discover

Usage in other scripts:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    import path_config
    paths = path_config.resolve()
    # paths["canvas_base"]  — Canvas API base URL (includes /api/v1)
    # paths["env_file"]     — Path to .env file
    # paths["courses"]["LTV"]["folder_path"] etc.
"""

import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ── Derived from this file's location — always correct after any rename/move ──
SCRIPTS_DIR     = Path(__file__).resolve().parent       # claude/scripts/  (or repo/scripts/)
CLAUDE_DIR      = SCRIPTS_DIR.parent                    # claude/          (or repo/)
PROMPTS_DIR     = CLAUDE_DIR / "prompts"                # claude/prompts/


def _early_env(key: str) -> str:
    """
    A setting needed before the full .env discovery below (which needs a root
    to search): the environment first, then a plain key=value scan of the
    .env next to this checkout or one level up.
    """
    raw = os.getenv(key, "")
    if raw:
        return raw
    for candidate in (CLAUDE_DIR / ".env", CLAUDE_DIR.parent / ".env"):
        try:
            if not candidate.exists():
                continue
            for line in candidate.read_text().splitlines():
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() == key:
                    return v.strip().strip('"').strip("'")
        except Exception:
            continue
    return ""


def _coursework_root() -> Path:
    """
    Where the course folders live.

    By default this is the parent of the scripts folder, which assumes the
    scripts have been copied to Coursework/claude/scripts/. Setting
    COURSEWORK_ROOT (env var, or a line in .env) points them at the folder
    directly, so a git clone can run in place with no second copy to keep
    in sync.
    """
    raw = _early_env("COURSEWORK_ROOT")
    return Path(raw).expanduser().resolve() if raw else CLAUDE_DIR.parent


def _config_file() -> Path:
    """
    canvas_config.json holds the course list and per-course folder names. It
    defaults to sitting next to this checkout, but when the scripts run from
    a throwaway checkout (CI) the coursework folder is the only thing that
    persists, so CANVAS_CONFIG_FILE can move it in there.
    """
    raw = _early_env("CANVAS_CONFIG_FILE")
    return Path(raw).expanduser().resolve() if raw else CLAUDE_DIR / "canvas_config.json"


COURSEWORK_ROOT = _coursework_root()
CONFIG_FILE     = _config_file()
MASTER_PROMPT   = PROMPTS_DIR / "cheat_sheet_prompt.md"

# ── Populated by resolve() — do not edit directly ────────────────────────────
# These are dicts so existing callers holding a reference still see updates.
CANVAS_IDS:   dict[str, int] = {}   # abbrev → Canvas course ID
COURSE_NAMES: dict[str, str] = {}   # abbrev → full course name
CANVAS_BASE:  str = ""              # e.g. "https://hbs.instructure.com/api/v1"

# ── Course cache TTL ──────────────────────────────────────────────────────────
_COURSE_TTL_HOURS = 24

# ── Config file I/O ───────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception:
        return {}


def _save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

# ── .env parsing ──────────────────────────────────────────────────────────────

def _read_env_file(env_file: "Path | None") -> dict[str, str]:
    """Read key=value pairs from an env file."""
    env: dict[str, str] = {}
    if not env_file or not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _get_canvas_base(env: dict[str, str]) -> str:
    """
    Return the canonical Canvas API base URL (always ends with /api/v1).
    Accepts CANVAS_BASE_URL (domain only) or CANVAS_API_URL (with /api/v1).
    """
    raw = env.get("CANVAS_BASE_URL", "") or env.get("CANVAS_API_URL", "")
    raw = raw.rstrip("/")
    if not raw:
        return ""
    # Normalise: strip /api/v1 if already present, then re-add
    if raw.endswith("/api/v1"):
        raw = raw[: -len("/api/v1")]
    return raw + "/api/v1"

# ── .env file discovery ───────────────────────────────────────────────────────

def _find_env_file() -> "Path | None":
    """
    The .env file containing CANVAS_API_TOKEN: next to the coursework, or
    next to this checkout. Nowhere else — a scan of ~/repos/*/.env and ~/.env
    used to be able to pick up another project's credentials.
    """
    for p in (COURSEWORK_ROOT / ".env", CLAUDE_DIR / ".env"):
        try:
            if p.exists() and "CANVAS_API_TOKEN" in p.read_text():
                return p.resolve()
        except Exception:
            continue
    return None

# ── Canvas course discovery ───────────────────────────────────────────────────

def _abbrev_from_course(course: dict, taken: set) -> str:
    """
    Derive a short, folder-safe abbreviation from a Canvas course object.

    Priority:
      1. First all-uppercase token (2–8 chars) in course_code
         e.g. "CATS 26F" → "CATS",  "MBA 600 26F" → "MBA"
      2. First alphabetic token from course_code, uppercased
      3. Initials from course name (articles/prepositions skipped)
      4. Fallback: "C{id}"
    """
    code = course.get("course_code", "")
    name = course.get("name", "")

    # 1. First all-caps word (2–8 letters)
    base = ""
    for m in re.finditer(r"\b([A-Z]{2,8})\b", code):
        base = m.group(1)
        break

    if not base:
        # 2. Strip non-alpha, take first word
        letters = re.sub(r"[^A-Za-z\s]", "", code).strip()
        parts = letters.split()
        base = parts[0].upper() if parts else ""

    if len(base) < 2:
        # 3. Initials from course name
        skip = {"a", "an", "the", "and", "or", "of", "for", "in", "on", "to", "at"}
        words = [w for w in name.split() if w.lower() not in skip and w[:1].isalpha()]
        base = "".join(w[0].upper() for w in words[:4]) or f"C{course['id']}"

    # Deduplicate: CATS → CATS2 → CATS3 ...
    if base not in taken:
        return base
    for i in range(2, 20):
        candidate = f"{base}{i}"
        if candidate not in taken:
            return candidate
    return f"C{course['id']}"


def _clean_course_name(raw: str) -> str:
    """
    Extract the human-readable name from Canvas's internal course title format.

    Canvas often formats titles as:  "ABBREV - 00 Full Course Name 1234"
    or:                              "ABBREV EXTRA - 00 Full Course Name (TAG) 1234"
    This strips the section prefix and trailing numeric/tag codes.
    If the pattern doesn't match (e.g. already a plain name), returns raw unchanged.
    """
    raw = raw.strip()
    # Split on the first " - " to isolate the abbreviation prefix
    parts = raw.split(" - ", 1)
    if len(parts) != 2:
        return raw
    rest = parts[1].strip()  # e.g. "00 Capitalism and the State 1120"
    # Strip leading section number (1–3 digits followed by a space)
    m = re.match(r"^\d{1,3}\s+(.+)", rest)
    if not m:
        return raw
    name = m.group(1).strip()  # e.g. "Capitalism and the State 1120"
    # Strip trailing 3–5 digit course code
    name = re.sub(r"\s+\d{3,5}\s*$", "", name).strip()
    # Strip trailing " (TAG)" suffix Canvas sometimes appends
    name = re.sub(r"\s+\([^)]{1,15}\)\s*$", "", name).strip()
    return name if name else raw


def _fetch_enrolled_courses(token: str, base_url: str) -> list:
    """
    Return active student-enrolled Canvas courses via the API, with each
    course's term (`include[]=term` → {id, name, start_at, end_at}), following
    pagination — a 101st course used to be dropped silently.
    """
    params = urlencode({
        "enrollment_type":  "student",
        "enrollment_state": "active",
        "include[]":        "term",
        "per_page":         "100",
    })
    url = f"{base_url}/courses?{params}"
    out: list = []
    try:
        while url:
            req = Request(url, headers={"Authorization": f"Bearer {token}"})
            with urlopen(req, timeout=15) as resp:
                out.extend(json.loads(resp.read()))
                link = resp.headers.get("Link", "")
            url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    m = re.search(r"<(.+?)>", part)
                    url = m.group(1) if m else None
    except URLError as e:
        print(f"  [paths] WARNING: Canvas course discovery failed ({e})")
    except Exception as e:
        print(f"  [paths] WARNING: Unexpected error during course discovery ({e})")
    return out


# ── Terms ─────────────────────────────────────────────────────────────────────

TERM_DIRS = {"fall", "spring", "summer", "winter", "january", "j-term", "jterm"}
_TERM_RE = re.compile(r"\b(fall|autumn|spring|summer|winter)\b", re.IGNORECASE)


def term_folder(term_name: "str | None") -> "str | None":
    """'Fall 2026' / '2026 Fall' / 'Autumn Term' → 'Fall'; 'Default Term' → None."""
    m = _TERM_RE.search(term_name or "")
    if not m:
        return None
    word = m.group(1).lower()
    return "Fall" if word in ("fall", "autumn") else word.title()


def term_end(course: dict) -> "str | None":
    return (course.get("term") or {}).get("end_at") or course.get("end_at")


def term_start(course: dict) -> "str | None":
    return (course.get("term") or {}).get("start_at") or course.get("start_at")


def _parse_iso(s: "str | None") -> "datetime | None":
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_active(entry: dict, now: "datetime | None" = None, grace_days: int = 30) -> bool:
    """
    A course still worth syncing: no known term end, or one less than
    grace_days ago. Ended courses stay resolvable for on-demand work but cost
    no API calls in the scheduled runs.
    """
    end = entry.get("term_end")
    end = end if isinstance(end, datetime) else _parse_iso(end)
    if end is None:
        return True
    now = now or datetime.now(timezone.utc)
    return end + timedelta(days=grace_days) >= now


def active_courses(courses: dict) -> dict:
    return {a: d for a, d in courses.items() if is_active(d)}


def terms(courses: dict) -> "dict[str, list[str]]":
    """{'Fall': ['MP', 'NEG'], 'Spring': [...]} for courses with a folder."""
    out: dict = {}
    for abbrev, d in courses.items():
        if not d.get("folder_path"):
            continue
        out.setdefault(d.get("term") or "Unsorted", []).append(abbrev)
    return {t: sorted(v) for t, v in out.items()}


def _should_refresh(cfg: dict) -> bool:
    """Return True if the course cache is missing, stale, or in the old format."""
    courses = cfg.get("courses", {})
    if not courses:
        return True
    # Old format: entries have no canvas_id (just folder_name)
    if any(not v.get("canvas_id") for v in courses.values()):
        return True
    last = cfg.get("courses_refreshed_at")
    if not last:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        return age >= timedelta(hours=_COURSE_TTL_HOURS)
    except Exception:
        return True


def _discover_courses(token: str, base_url: str, cfg: dict) -> "tuple[dict, bool]":
    """
    Fetch enrolled courses from Canvas and merge them into the existing config.

    Merging, not replacing: Canvas has been observed returning a partial
    enrolment list (10 courses one hour, 6 the next, with no `rel="next"` page
    to follow). Rebuilding the map from a single response deletes every course
    missing from it, and a dropped course stops syncing silently — taking a
    term of session folders out of scope with it. Removing a course you have
    genuinely dropped is a one-line edit to canvas_config.json; losing one
    without noticing is not.

    Honours `abbrev_overrides` in canvas_config.json — {canvas_id: "ABBREV"} —
    so a course can use your folder name rather than the code Canvas reports.

    Returns (courses_dict, changed).
    """
    print("  [paths] Refreshing course list from Canvas...")
    raw = _fetch_enrolled_courses(token, base_url)
    if not raw:
        return cfg.get("courses", {}), False

    existing  = cfg.get("courses", {})
    overrides = {str(k): v for k, v in cfg.get("abbrev_overrides", {}).items()}
    term_overrides = {str(k): v for k, v in cfg.get("term_folders", {}).items()}
    end_overrides  = {str(k): v for k, v in cfg.get("term_ends", {}).items()}

    merged: dict = {a: dict(e) for a, e in existing.items()}
    by_id: dict  = {str(e["canvas_id"]): a
                    for a, e in existing.items() if e.get("canvas_id")}
    taken: set   = set(merged)   # so a new course can't take a known abbrev
    seen: list   = []

    for c in raw:
        if not isinstance(c, dict) or "id" not in c:
            continue
        cid = str(c["id"])
        # An explicit override wins, then the abbrev this course already uses,
        # then a freshly derived one deduped against everything known.
        abbrev = overrides.get(cid) or by_id.get(cid) or _abbrev_from_course(c, taken)
        taken.add(abbrev)
        old  = merged.get(abbrev, {})
        term = c.get("term") or {}
        # Spread the old entry first so folder_name and any hand-set keys
        # survive; the term is remembered even after Canvas stops returning
        # the course (enrollment_state=active drops it once the term ends),
        # which is what lets January tell Fall from Spring.
        merged[abbrev] = {
            **old,
            "canvas_id":  c["id"],
            "full_name":  _clean_course_name(c.get("name", abbrev)),
            "term_id":    term.get("id", old.get("term_id")),
            "term_name":  term.get("name", old.get("term_name")),
            "term":       term_overrides.get(cid) or term_folder(term.get("name")) or old.get("term"),
            "term_start": term_start(c) or old.get("term_start"),
            "term_end":   end_overrides.get(cid) or term_end(c) or old.get("term_end"),
        }
        seen.append(abbrev)

    # Term overrides apply to every known course, returned this time or not.
    for abbrev, entry in merged.items():
        cid = str(entry.get("canvas_id"))
        if cid in term_overrides:
            entry["term"] = term_overrides[cid]
        if cid in end_overrides:
            entry["term_end"] = end_overrides[cid]

    # Apply overrides across the whole map rather than only this response, so a
    # course Canvas didn't return still ends up under the abbrev you chose.
    for abbrev in list(merged):
        cid  = str(merged[abbrev].get("canvas_id"))
        want = overrides.get(cid)
        if not want or want == abbrev:
            continue
        if want not in merged:
            merged[want] = merged.pop(abbrev)
        elif str(merged[want].get("canvas_id")) == cid:
            # This response already wrote the course under `want`; keep that
            # entry but don't lose a folder we had already resolved.
            if not merged[want].get("folder_name"):
                merged[want]["folder_name"] = merged[abbrev].get("folder_name")
            del merged[abbrev]
        else:
            print(f"  [paths] WARNING: abbrev_overrides wants {abbrev} → {want}, "
                  f"but {want} is already course {merged[want].get('canvas_id')} — skipping")
            continue
        if abbrev in seen:
            seen[seen.index(abbrev)] = want
        print(f"  [paths] {abbrev} → {want} (abbrev_overrides)")

    print(f"  [paths] Found {len(seen)} enrolled course(s): {', '.join(sorted(seen))}")
    held = sorted(set(merged) - set(seen))
    if held:
        print(f"  [paths] Canvas did not return {len(held)} known course(s) this time; "
              f"keeping them: {', '.join(held)}")
    return merged, True

# ── Course folder resolution ──────────────────────────────────────────────────

_STOPWORDS = {"a", "an", "the", "and", "or", "of", "for", "in", "on", "to", "at", "with"}


def _words(s: str) -> list:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOPWORDS]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _match_in(dir_: Path, abbrev: str, full_name: str) -> "str | None":
    """
    The child of dir_ that is this course, or None. Exact name first, then
    case/space-insensitive, then every non-stopword word of the full name
    present as a whole word (or difflib ≥ 0.85), then the abbreviation as a
    whole word — so "IP" cannot match "Seminar in Investing".
    """
    import difflib
    if not dir_.is_dir():
        return None
    kids = [d for d in dir_.iterdir()
            if d.is_dir() and not d.name.startswith(".")
            and d.name.lower() not in {"claude", "overview"} | TERM_DIRS]
    # Exact match by name string, not is_dir(): macOS's case-insensitive
    # filesystem would otherwise report "Negotiations" for "negotiations".
    for d in kids:
        if full_name and d.name == _folder_safe(full_name):
            return d.name
    for d in kids:
        if full_name and _norm(d.name) == _norm(full_name):
            return d.name
        if _norm(d.name) == _norm(abbrev):
            return d.name
    want = _words(full_name)
    for d in kids:
        have = set(_words(d.name))
        if want and all(w in have for w in want):
            return d.name
        if full_name and difflib.SequenceMatcher(None, _norm(d.name), _norm(full_name)).ratio() >= 0.85:
            return d.name
    abbrev_words = _words(abbrev)
    for d in kids:
        have = set(re.findall(r"[a-z0-9]+", d.name.lower()))
        if abbrev_words and all(w in have for w in abbrev_words):
            return d.name
    return None


def _find_course_folder(abbrev: str, cached: "str | None" = None,
                        full_name: str = "", term: "str | None" = None) -> "str | None":
    """
    Find the course's folder, relative to COURSEWORK_ROOT ("Fall/Motivating
    People"). Priority: cached name → within the term folder: exact full
    name → case-insensitive → fuzzy → abbreviation. A course is never matched
    to another term's folder; only a course with no known term falls back to
    scanning the root (the pre-term layout).
    """
    if cached and (COURSEWORK_ROOT / cached).is_dir():
        return cached
    if term:
        hit = _match_in(COURSEWORK_ROOT / term, abbrev, full_name)
        return f"{term}/{hit}" if hit else None
    return _match_in(COURSEWORK_ROOT, abbrev, full_name)


def _folder_safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(". ")


def default_folder_name(abbrev: str, full_name: "str | None",
                        term: "str | None" = None) -> str:
    """
    "Fall/Seminar in Investing": the term folder, then the course's full
    name — what a human looks for. The abbreviation still appears inside the
    folder, in file prefixes like "260915 INVS Podcast.m4a".
    """
    if full_name and full_name.strip() and full_name.strip() != abbrev:
        base = _folder_safe(full_name.strip())
    else:
        base = abbrev
    return f"{term}/{base}" if term else base

# ── Main resolution function ──────────────────────────────────────────────────

_ENV_OVERRIDES = ("CANVAS_API_TOKEN", "CANVAS_BASE_URL", "CANVAS_API_URL")


def _no_create_folders() -> bool:
    """
    CANVAS_NO_CREATE_FOLDERS=1 makes every resolve() in the process read-only.

    Six scripts call resolve() at import time with folder creation on, so a
    report-only tool that imports one of them would otherwise create course
    folders as a side effect of `import`.
    """
    return os.getenv("CANVAS_NO_CREATE_FOLDERS", "").strip().lower() not in ("", "0", "false", "no")


def resolve(create_folders: "bool | None" = None) -> dict:
    """
    Resolve all paths, auto-discover Canvas courses if the cache is stale,
    and update canvas_config.json when anything changes.

    Populates the module-level CANVAS_IDS, COURSE_NAMES, and CANVAS_BASE
    dicts/strings so existing callers holding references see the updates.

    create_folders=False only reports; --discover uses it to show what the
    first real run would do. Left unset it follows CANVAS_NO_CREATE_FOLDERS,
    so a read-only tool can suppress creation process-wide before importing
    the scripts that resolve at import time.

    Returns:
        {
          "canvas_base":     str,          # e.g. "https://hbs.instructure.com/api/v1"
          "env_file":        Path | None,
          "coursework_root": Path,
          "prompts_dir":     Path,
          "master_prompt":   Path,
          "courses": {
            "LTV": {
              "canvas_id":         17019,
              "full_name":         "Launching Tech Ventures in the Age of AI",
              "folder_name":       "LTV",
              "folder_path":       Path(...),
              "refinement_prompt": Path(...) | None,
            },
            ...
          }
        }
    """
    global CANVAS_BASE
    if create_folders is None:
        create_folders = not _no_create_folders()
    cfg = _load_config()
    changed = False

    # ── .env file ─────────────────────────────────────────────────────────────
    # With the token in the environment (CI secrets) there may be no .env at
    # all; don't warn about that, and don't record a None that a Mac run would
    # flip back on its next commit.
    from_env = bool(os.getenv("CANVAS_API_TOKEN"))
    env_file = _find_env_file()
    new_env_str = str(env_file) if env_file else None
    if not from_env and new_env_str != cfg.get("env_file"):
        if env_file:
            print(f"  [paths] env_file → {env_file}")
        else:
            print("  [paths] WARNING: .env file not found — API calls will fail")
        cfg["env_file"] = new_env_str
        changed = True

    # ── Canvas base URL ────────────────────────────────────────────────────────
    env_vars = _read_env_file(env_file)
    for key in _ENV_OVERRIDES:            # environment beats the file
        if os.getenv(key):
            env_vars[key] = os.getenv(key)
    canvas_base = _get_canvas_base(env_vars)
    token = env_vars.get("CANVAS_API_TOKEN", "")

    if canvas_base != cfg.get("canvas_base_url"):
        cfg["canvas_base_url"] = canvas_base
        changed = True

    CANVAS_BASE = canvas_base  # expose at module level

    # ── Course discovery ───────────────────────────────────────────────────────
    if _should_refresh(cfg):
        if canvas_base and token:
            discovered, disc_changed = _discover_courses(token, canvas_base, cfg)
            if disc_changed:
                cfg["courses"] = discovered
                cfg["courses_refreshed_at"] = datetime.now(timezone.utc).isoformat()
                changed = True
        else:
            if not token:
                print("  [paths] WARNING: CANVAS_API_TOKEN not set — cannot discover courses")
            if not canvas_base:
                print("  [paths] WARNING: CANVAS_BASE_URL not set — cannot discover courses")

    if "courses" not in cfg:
        cfg["courses"] = {}
        changed = True

    # ── Folder resolution + module-level dicts ─────────────────────────────────
    CANVAS_IDS.clear()
    COURSE_NAMES.clear()
    courses: dict = {}

    ignored = {str(i) for i in cfg.get("ignored_courses", [])}

    for abbrev, entry in cfg.get("courses", {}).items():
        canvas_id = entry.get("canvas_id")
        if not canvas_id:
            continue
        if str(canvas_id) in ignored:
            continue   # listed in ignored_courses — e.g. an admin or kickoff shell

        full_name = entry.get("full_name", abbrev)
        term      = entry.get("term")

        cached_name = entry.get("folder_name")
        folder_name = _find_course_folder(abbrev, cached_name, full_name, term)
        if folder_name != cached_name:
            print(f"  [paths] {abbrev}: folder {cached_name!r} → {folder_name!r}")
            cfg["courses"][abbrev]["folder_name"] = folder_name
            changed = True

        # A course with no folder used to be dropped from every loop in
        # canvas_refresh, which meant it never got a folder created and so could
        # never resolve on a later run either — a silent, permanent dead end.
        # Create the folder so the course joins the sync from here on. A name
        # already chosen in the config (seeded by hand, or by an earlier run
        # whose empty folder git did not keep) wins over the derived default.
        if folder_name is None:
            want = cached_name or default_folder_name(abbrev, full_name, term)
            new_dir = COURSEWORK_ROOT / want
            if not create_folders:
                print(f"  [paths] {abbrev}: would create course folder {new_dir}")
            else:
                try:
                    new_dir.mkdir(parents=True, exist_ok=True)
                    folder_name = want
                    cfg["courses"][abbrev]["folder_name"] = folder_name
                    changed = True
                    print(f"  [paths] {abbrev}: created course folder {new_dir}")
                    print(f"  [paths]   (add {canvas_id} to \"ignored_courses\" in "
                          f"canvas_config.json to skip this course instead)")
                except OSError as e:
                    print(f"  [paths] WARNING: could not create folder for {abbrev}: {e}")
        code = abbrev.replace(" ", "_")
        refinement = PROMPTS_DIR / f"cheat_sheet_prompt_{code}_refinement.md"
        folder_path = (COURSEWORK_ROOT / folder_name) if folder_name else None

        CANVAS_IDS[abbrev]   = canvas_id
        COURSE_NAMES[abbrev] = full_name
        courses[abbrev] = {
            "canvas_id":         canvas_id,
            "full_name":         full_name,
            "folder_name":       folder_name,
            "folder_path":       folder_path,
            "refinement_prompt": refinement if refinement.exists() else None,
            "term":              term,
            "term_name":         entry.get("term_name"),
            "term_end":          _parse_iso(entry.get("term_end")),
        }

    if not courses and not token:
        print("  [paths] WARNING: No courses found. Add CANVAS_BASE_URL and")
        print("          CANVAS_API_TOKEN to your .env file and re-run.")

    if changed:
        _save_config(cfg)

    return {
        "canvas_base":     canvas_base,
        "env_file":        env_file,
        "coursework_root": COURSEWORK_ROOT,
        "prompts_dir":     PROMPTS_DIR,
        "master_prompt":   MASTER_PROMPT,
        "courses":         courses,
    }


# ── Bootstrap helper ──────────────────────────────────────────────────────────

def _discover_cli() -> None:
    """
    Show the courses Canvas returns and the folder each would get, creating
    nothing. Run it, edit abbrev_overrides / ignored_courses / folder_name in
    canvas_config.json, run it again, then do the first real sync.
    """
    paths = resolve(create_folders=False)
    print(f"\n  Coursework root: {COURSEWORK_ROOT}")
    print(f"  Config file:     {CONFIG_FILE}\n")
    if not paths["courses"]:
        print("  No courses. Check CANVAS_API_TOKEN / CANVAS_BASE_URL.\n")
        return
    raw_terms = sorted({str(i.get("term_name")) for i in paths["courses"].values()})
    print(f"  Canvas term names seen: {', '.join(raw_terms)}\n")
    rows = [("ABBREV", "CANVAS ID", "TERM", "ENDS", "FOLDER", "FULL NAME")]
    warn = []
    for abbrev, info in sorted(paths["courses"].items()):
        term = info.get("term") or "?"
        if term == "?":
            warn.append(f'{abbrev}: no term recognised in "{info.get("term_name")}" '
                        f'— set "term_folders": {{"{info["canvas_id"]}": "Fall"}}')
        end = info.get("term_end")
        ends = end.strftime("%Y-%m-%d") if end else "?"
        folder = info["folder_name"] or \
            f"(would create) {default_folder_name(abbrev, info['full_name'], info.get('term'))}"
        rows.append((abbrev, str(info["canvas_id"]), term, ends, folder, info["full_name"]))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)))
    for w in warn:
        print(f"\n  WARN {w}")
    print(f"""
  To change an abbreviation:  "abbrev_overrides": {{"<canvas id>": "INVS"}}
  To skip a course:           "ignored_courses":  [<canvas id>]
  To pick a folder name:      set "folder_name" on the course entry (e.g. "Fall/Negotiations")
  To fix a term:              "term_folders": {{"<canvas id>": "Spring"}}, "term_ends": {{"<canvas id>": "2026-12-20"}}
  ...in {CONFIG_FILE}, then run this again.
""")


if __name__ == "__main__":
    import sys
    if "--discover" in sys.argv:
        _discover_cli()
    else:
        print(__doc__)
