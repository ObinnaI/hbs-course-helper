#!/usr/bin/env python3
"""
participation_tracker.py — HBS participation tracker spreadsheet.

Creates/refreshes: <COURSEWORK_ROOT>/Participation Tracker.xlsx
  - Single worksheet ("Participation")
  - Every course side by side (alphabetical), each a different color
  - Narrow separator column between each course group
  - Row 1: course name header (dark course color, merged)
  - Row 2: live participation rate "X / Y" (mid course color, merged)
  - Row 3: column labels Day | Case Title | Rating
  - Row 4+: session rows sorted by date

Rating values: ok, good, great, x (didn't speak), or blank (not yet entered)
Denominator = count of non-blank rating cells (entry-based)
Numerator   = count of ok + good + great ratings

On refresh (called from canvas_refresh.py --weekly):
  - Case titles and dates are updated from Canvas
  - User-entered ratings are preserved (keyed by course + session date)

Run standalone:
  ./.venv/bin/python scripts/participation_tracker.py
"""

import re
import sys
from datetime import datetime, timedelta, date as _date
from zoneinfo import ZoneInfo
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent))
import path_config
import canvas_common

_paths       = path_config.resolve()
DEST_ROOT    = _paths["coursework_root"]
_COURSES     = _paths["courses"]
COURSE_NAMES = path_config.COURSE_NAMES

# Canvas due dates are wall-clock Boston time. ZoneInfo handles the EDT->EST
# switch in early November; a fixed -4 offset silently shifted every date
# bucket by an hour for the rest of the term.
BOSTON = ZoneInfo("America/New_York")

COLS_PER_COURSE = 3
STRIDE          = COLS_PER_COURSE + 1   # 4: three data cols + one separator col
DATA_START_ROW  = 4                     # rows 1-3 are header rows
TRACKER_NAME    = "Participation Tracker.xlsx"
LEGACY_FILE     = DEST_ROOT / TRACKER_NAME   # pre-term layout: one sheet at the root
TERM_OVER_DAYS  = 30                         # leave a term's sheet alone this long after its last class


def tracker_path(term: str) -> Path:
    """One sheet per term: <root>/Fall/Participation Tracker.xlsx."""
    return DEST_ROOT / term / TRACKER_NAME

# Per-course color scheme: (header_dark, rate_mid, even_row_light). Courses
# take these in alphabetical order and wrap around; the set used to be keyed
# by four specific course codes, which gave anyone else an empty sheet.
_PALETTE = [
    ("1E6B4A", "2A9466", "E8F6EF"),   # teal / green
    ("1F4E79", "2E75B6", "EBF3FB"),   # navy / blue
    ("7B2133", "B03050", "FAEAED"),   # burgundy / red
    ("4A2178", "6B33A8", "F1ECF9"),   # indigo / purple
    ("7A4A00", "B37400", "FBF3E4"),   # amber
    ("2F3E4E", "4F6B85", "EDF1F5"),   # slate
]


def course_order(term: "str | None" = None) -> list[str]:
    """Every course with a folder (in this term, if given), alphabetical."""
    return sorted(a for a, d in _COURSES.items()
                  if d.get("folder_path") and (term is None or d.get("term") == term))


def course_colors(order: list[str]) -> dict[str, tuple[str, str, str]]:
    return {a: _PALETTE[i % len(_PALETTE)] for i, a in enumerate(order)}


def course_start_col(i: int) -> int:
    """1-indexed start column for course i: 1, 5, 9, 13."""
    return i * STRIDE + 1


def sep_col_for(i: int) -> int:
    """1-indexed separator column that follows course i (i = 0, 1, 2)."""
    return i * STRIDE + COLS_PER_COURSE + 1   # 4, 8, 12


# ── Helpers ───────────────────────────────────────────────────────────────────


def boston_date(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(BOSTON)


def yymmdd(dt: datetime) -> str:
    return dt.strftime("%y%m%d")


def col_letter(n: int) -> str:
    """1-indexed column number → Excel column letter (A, B, …, Z, AA, …)."""
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


extract_case_title = canvas_common.extract_case_title


# ── Session data ──────────────────────────────────────────────────────────────


def get_all_sessions(order: list[str]) -> dict[str, list[dict]]:
    """
    Fetch every class session for each course, chronologically.
    Returns {abbrev: [assignment_dict, ...]}.

    Quizzes and uploads are not participation opportunities and are left out.
    """
    # Lazy: canvas_refresh imports this module at top level, and the two other
    # modules in the same position already form import cycles that only work
    # because they touch canvas_refresh at call time. Reusing its canvas_get
    # also gets the network-error handling the local copy never had.
    import canvas_refresh as cr

    result: dict[str, list[dict]] = {}
    for abbrev in order:
        course_id = _COURSES[abbrev]["canvas_id"]
        assignments = cr.canvas_get(f"courses/{course_id}/assignments", {"per_page": 100})
        sessions = [a for a in assignments
                    if a.get("due_at")
                    and cr._kind_matches(cr.posting_kind(a), cr.SESSION_KINDS)]
        sessions.sort(key=lambda a: a["due_at"])
        result[abbrev] = sessions
    return result


# ── Preserve existing ratings ─────────────────────────────────────────────────


def read_existing_ratings(path: Path, order: list[str]) -> dict[str, dict[str, str]]:
    """
    Read user-entered ratings from an existing spreadsheet.
    Returns {abbrev: {yymmdd_key: rating_string}}.

    Column groups are matched to courses by the full name in their row-1
    header, not by position: with an alphabetical layout, a course added
    mid-term can sort in front of the others and shift every group right,
    and positional reading would then hand each course its neighbour's
    ratings. Rows are identified by the date value in the Day column.
    """
    ratings: dict[str, dict[str, str]] = {a: {} for a in order}
    try:
        import openpyxl
    except ImportError:
        return ratings

    by_header = {COURSE_NAMES.get(a, a): a for a in order}
    by_header.update({a: a for a in order})     # a sheet written with bare codes

    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        i = 0
        while True:
            day_col    = course_start_col(i)
            rating_col = day_col + 2
            header     = ws.cell(row=1, column=day_col).value
            if header is None or str(header).strip() == "":
                break
            abbrev = by_header.get(str(header).strip())
            i += 1
            if abbrev is None:
                print(f"  Warning: column group '{header}' matches no current course "
                      f"— its ratings are not carried over")
                continue

            for row in ws.iter_rows(min_row=3):
                if len(row) < rating_col:
                    continue
                day_val    = row[day_col - 1].value
                rating_val = row[rating_col - 1].value

                if day_val is None:
                    continue
                if isinstance(day_val, datetime):
                    day_val = day_val.date()
                if not isinstance(day_val, _date):
                    continue

                key    = day_val.strftime("%y%m%d")
                rating = str(rating_val or "").strip().lower()
                if rating:
                    ratings[abbrev][key] = rating
    except Exception as e:
        print(f"  Warning: could not read existing ratings ({e}) — starting fresh")

    return ratings


# ── Spreadsheet builder ───────────────────────────────────────────────────────


def build_tracker(term: str, order: "list[str] | None" = None,
                  sessions: "dict | None" = None):
    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, PatternFill
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError:
        sys.exit("openpyxl not installed.\nRun: ./.venv/bin/pip install openpyxl")

    OUTPUT_FILE = tracker_path(term)
    order  = order if order is not None else course_order(term)
    colors = course_colors(order)

    # ── Preserve existing user ratings ────────────────────────────────────────
    # A term's first sheet inherits from the old single root sheet, if any.
    existing: dict[str, dict[str, str]] = {a: {} for a in order}
    source = OUTPUT_FILE if OUTPUT_FILE.exists() else (LEGACY_FILE if LEGACY_FILE.exists() else None)
    if source is not None:
        existing = read_existing_ratings(source, order)
        total = sum(len(v) for v in existing.values())
        if total:
            print(f"  Preserved {total} existing rating(s) from {source.name}.")

    # ── Fetch Canvas sessions ─────────────────────────────────────────────────
    if sessions is None:
        print("  Fetching sessions from Canvas...")
        sessions = get_all_sessions(order)
    for abbrev in order:
        print(f"    {abbrev}: {len(sessions.get(abbrev, []))} session(s)")

    max_sessions = max((len(v) for v in sessions.values()), default=0)
    # Formula ranges extend a bit past the last data row for future sessions
    formula_end = DATA_START_ROW + max(max_sessions, 50) + 10

    # ── Build workbook ────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Participation"

    WHITE  = "FFFFFF"
    center   = Alignment(horizontal="center", vertical="center")
    wrap_top = Alignment(wrap_text=True, vertical="top")

    # ── Column widths ─────────────────────────────────────────────────────────
    # Day=11, Case Title=42, Rating=10 per course; separator=2
    col_widths = [11, 42, 10]
    for i in range(len(order)):
        sc = course_start_col(i)
        for j, w in enumerate(col_widths):
            ws.column_dimensions[col_letter(sc + j)].width = w
        if i < len(order) - 1:
            ws.column_dimensions[col_letter(sep_col_for(i))].width = 2

    # ── Row heights ────────────────────────────────────────────────────────────
    ws.row_dimensions[1].height = 24   # course name
    ws.row_dimensions[2].height = 22   # participation rate
    ws.row_dimensions[3].height = 18   # column labels

    # ── Per-course headers (rows 1–3) ─────────────────────────────────────────
    for i, abbrev in enumerate(order):
        sc        = course_start_col(i)
        ec        = sc + COLS_PER_COURSE - 1
        rating_c  = col_letter(sc + 2)   # A+2, E+2 etc → C, G, K, O
        full_name = COURSE_NAMES.get(abbrev, abbrev)

        dark, mid, _ = colors[abbrev]
        dark_fill = PatternFill(fill_type="solid", fgColor=dark)
        mid_fill  = PatternFill(fill_type="solid", fgColor=mid)
        bold_white = Font(bold=True, color=WHITE, size=12)

        # ── Row 1: Course name ────────────────────────────────────────────────
        ws.merge_cells(start_row=1, start_column=sc, end_row=1, end_column=ec)
        c1 = ws.cell(row=1, column=sc, value=full_name)
        c1.font      = bold_white
        c1.fill      = dark_fill
        c1.alignment = center

        # ── Row 2: Live participation rate "spoke / entered" ──────────────────
        num_f = (
            f"SUMPRODUCT(({rating_c}{DATA_START_ROW}:{rating_c}{formula_end}=\"ok\")"
            f"+({rating_c}{DATA_START_ROW}:{rating_c}{formula_end}=\"good\")"
            f"+({rating_c}{DATA_START_ROW}:{rating_c}{formula_end}=\"great\"))"
        )
        den_f = (
            f"SUMPRODUCT(({rating_c}{DATA_START_ROW}:{rating_c}{formula_end}<>\"\"))"
        )
        rate_formula = f'=TEXT({num_f},"0")&" / "&TEXT({den_f},"0")'

        ws.merge_cells(start_row=2, start_column=sc, end_row=2, end_column=ec)
        c2 = ws.cell(row=2, column=sc, value=rate_formula)
        c2.font      = bold_white
        c2.fill      = mid_fill
        c2.alignment = center

        # ── Row 3: Column labels ──────────────────────────────────────────────
        for j, label in enumerate(["Day", "Case Title", "Rating"]):
            c3 = ws.cell(row=3, column=sc + j, value=label)
            c3.font      = bold_white
            c3.fill      = dark_fill
            c3.alignment = center

    # ── Data rows (row 4+) ────────────────────────────────────────────────────
    for i, abbrev in enumerate(order):
        sc = course_start_col(i)
        _, _, light = colors[abbrev]
        even_fill = PatternFill(fill_type="solid", fgColor=light)

        course_sessions = sessions.get(abbrev, [])
        existing_abbrev = existing.get(abbrev, {})

        for row_offset, a in enumerate(course_sessions):
            row      = DATA_START_ROW + row_offset
            dt       = boston_date(a["due_at"])
            date_val = dt.date()
            date_key = yymmdd(dt)
            title    = extract_case_title(a.get("name", ""))
            rating   = existing_abbrev.get(date_key, "")

            bg = even_fill if (row_offset % 2 == 1) else None

            # Day
            day_cell = ws.cell(row=row, column=sc, value=date_val)
            day_cell.number_format = "MMM D"
            day_cell.alignment     = center
            day_cell.font          = Font(size=12)
            if bg:
                day_cell.fill = bg

            # Case title
            title_cell = ws.cell(row=row, column=sc + 1, value=title)
            title_cell.alignment = wrap_top
            title_cell.font      = Font(size=12)
            if bg:
                title_cell.fill = bg

            # Rating (user-filled)
            rating_cell = ws.cell(row=row, column=sc + 2, value=rating)
            rating_cell.alignment = center
            rating_cell.font      = Font(size=12)
            if bg:
                rating_cell.fill = bg

        # ── Dropdown validation for Rating column ─────────────────────────────
        rating_col_letter = col_letter(sc + 2)
        dv_end = DATA_START_ROW + max(len(course_sessions), 40)
        dv = DataValidation(
            type="list",
            formula1='"ok,good,great,x"',
            allow_blank=True,
            showDropDown=False,
            error="Enter ok, good, great, or x",
            errorTitle="Invalid entry",
            prompt="ok = brief comment\ngood = solid contribution\ngreat = strong insight\nx = didn't speak",
            promptTitle="Participation",
        )
        ws.add_data_validation(dv)
        dv.sqref = f"{rating_col_letter}{DATA_START_ROW}:{rating_col_letter}{dv_end}"

    # ── Conditional formatting: color-code ratings ────────────────────────────
    # great = green  good = light green  ok = yellow  x = gray
    rating_colors = [
        ("great", "C6EFCE", "375623", True),
        ("good",  "E2EFDA", "375623", False),
        ("ok",    "FFEB9C", "9C5700", False),
        ("x",     "F2F2F2", "7F7F7F", False),
    ]
    for i in range(len(order)):
        sc       = course_start_col(i)
        rating_c = col_letter(sc + 2)
        rng      = f"{rating_c}{DATA_START_ROW}:{rating_c}{formula_end}"

        for value, bg, fg, bold in rating_colors:
            fill = PatternFill(fill_type="solid", fgColor=bg)
            font = Font(color=fg, bold=bold, size=12)
            rule = CellIsRule(operator="equal", formula=[f'"{value}"'],
                              fill=fill, font=font)
            ws.conditional_formatting.add(rng, rule)

    # ── Freeze top 3 header rows ──────────────────────────────────────────────
    ws.freeze_panes = f"A{DATA_START_ROW}"

    # ── Save ──────────────────────────────────────────────────────────────────
    try:
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        wb.save(OUTPUT_FILE)
    except PermissionError:
        sys.exit(
            f"\nCould not save — is {OUTPUT_FILE.name} open in Excel? "
            "Close it and try again."
        )
    print(f"  Saved: {OUTPUT_FILE}")


# ── Public entry point (called from canvas_refresh.py) ───────────────────────


def refresh(now: "datetime | None" = None):
    """
    Refresh one tracker per term. Called from canvas_refresh.py --weekly.

    A term whose last class was more than TERM_OVER_DAYS ago is left exactly
    as it is — the ratings in it are a record, not something to rebuild.
    """
    now = now or datetime.now(tz=BOSTON)
    by_term = path_config.terms(_COURSES)
    if not by_term:
        print("  No courses with folders — nothing to build.")
        return
    for term, order in sorted(by_term.items()):
        print(f"  {term}: fetching sessions from Canvas...")
        sessions = get_all_sessions(order)
        dues = [boston_date(a["due_at"]) for v in sessions.values() for a in v]
        if not dues:
            print(f"  {term}: no class sessions posted yet — skipping.")
            continue
        if max(dues) < now - timedelta(days=TERM_OVER_DAYS):
            print(f"  {term}: term over, tracker left as is.")
            continue
        build_tracker(term, order=order, sessions=sessions)


# ── Standalone ───────────────────────────────────────────────────────────────


def main():
    print("Building HBS participation tracker(s)...")
    refresh()
    print("Done.")


if __name__ == "__main__":
    main()
