#!/usr/bin/env python3
"""
HBS Cheat Sheet Generator
Generates the case prep Notes for one class session, on demand.

Output: YYMMDD CLASSCODE Notes.docx and Notes.md  (saved in the session folder)

Usage:
  ./.venv/bin/python scripts/cheat_sheet.py 260902 LTV
  ./.venv/bin/python scripts/cheat_sheet.py 260908 CATS

How it works:
  1. Finds the session folder <COURSEWORK_ROOT>/LTV/260902 LTV/
  2. Pulls the Canvas class posting(s) for that day (discussion questions)
  3. Hands both to canvas_refresh.generate_notes — the same code the
     scheduled run uses, so readings, token budgets, and the prompt are
     handled identically here
  4. Always regenerates: "on demand" means now, whatever the staleness check
     would say

Adding readings: drop PDFs into the session folder before running.
Canvas PDFs are synced automatically; HBS case PDFs must be added manually.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import canvas_refresh as cr
import canvas_common


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    date_str = sys.argv[1]                    # e.g. 260902
    abbrev   = " ".join(sys.argv[2:]).upper()  # e.g. LTV  or  ENT FIN

    if abbrev not in cr.COURSES:
        sys.exit(f"Unknown course '{abbrev}'. Known: {', '.join(cr.COURSES)}")

    print(f"\nCheat sheet: {abbrev} {date_str}")
    print("Fetching Canvas assignment...", end=" ", flush=True)
    session = cr.build_session(abbrev, date_str)
    if session["assignments"]:
        print("found: " + "; ".join(a.get("name", "") for a in session["assignments"]))
    else:
        print("not found (will rely on readings only)")

    course_folder = cr._COURSES.get(abbrev, {}).get("folder_path") or cr.DEST_ROOT / abbrev
    session_dir   = canvas_common.session_dir_for(course_folder, date_str, abbrev,
                                                  session["assignments"])
    print(f"Session folder: {session_dir}")
    readings = cr._reading_files(session_dir)
    print(f"Readings found: {len(readings)}")
    for f in readings:
        print(f"  • {f.name}")

    if not readings and not session["assignments"]:
        sys.exit("No readings and no Canvas assignment found. Nothing to generate.")

    print(f"\nCalling Claude ({cr.MODEL})...", flush=True)
    cr.generate_notes(session)

    output_file = canvas_common.notes_paths(
        session_dir, date_str, abbrev, canvas_common.session_title(session["assignments"])).docx
    print(f"\n✅ Saved: {output_file}")
    print(f"   Open: open '{output_file}'")


if __name__ == "__main__":
    main()
