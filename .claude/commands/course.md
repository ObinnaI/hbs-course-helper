---
description: Load a course's knowledge base (brief, lenses, materials) for ad-hoc work
argument-hint: <course abbreviation or folder name>
---

Find the course folder for "$ARGUMENTS" under the coursework root (`COURSEWORK_ROOT` in `.env`, term folders `Fall/`, `Spring/`; match on the abbreviation in `claude/canvas_config.json` or the folder name).

Then:
1. Read its `CLAUDE.md` and `<materials folder>/Course Brief.md` in full.
2. List the class-day folders (`YYMMDD Class N - Title`) with a one-line note each: readings present, whether a cheat sheet exists, anything posted after class.
3. Summarise in ≤ 10 lines: where the course is, the lenses introduced so far, open threads.
4. Ask what I want to do — e.g. draft a cheat sheet, apply an earlier lens to a new case, compare two classes — and use the brief and the class folders as the source of truth. Cite the class a lens came from.
