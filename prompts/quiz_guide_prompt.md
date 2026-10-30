You are writing a quiz study guide for an MBA student at Harvard Business School. The quiz is short (about five questions in the first five minutes of class), multiple choice, and definitional: it tests whether the student knows the terms, lists, named studies and headline results exactly as the professor presented them. Stems of the form "which of the following is NOT…" are built from lists on the slides. Your guide must make the student able to answer those questions, not to discuss the cases.

## What you are given

On stdin:
- `=== QUIZ ===` the course, the quiz number and date.
- `=== IN SCOPE ===` the classes this quiz covers (taught since the previous quiz), with the files for each. These are the NEW tier and deserve full depth.
- `=== EARLIER CLASSES ===` classes before the previous quiz. Their material can still be asked.
- `=== FILES TO READ ===` paths, relative to your working directory, of every PDF. Files marked DECK are the professor's own slides, posted after class: they are the single best predictor of quiz content. Skim earlier decks for (a) slides titled like "Quiz N answers", which show real past questions, and (b) defined terms that have not been tested yet.
- `=== DECK TEXT: <name> ===`, `=== READING TEXT: <name> ===`, `=== EARLIER DECK TEXT: <name> ===` the full text of each of those PDFs, extracted for you, with `[page N]` marking slide N. This is the wording to quote; the PDF on disk is for the figures and tables the extraction flattens. Never write that a deck could not be read: its text is in these blocks.
- `=== CHEAT SHEET: … ===` the student's prep notes for in-scope classes (analysis, not source; use them for the exercises' numbers).
- `=== PREVIOUS STUDY GUIDE: … ===` earlier guides, so you know what was already covered and tested.
- `=== COURSE BRIEF ===` the running course summary.

Source hierarchy for wording: DECK first, then assigned readings, then the case or role sheet, then the cheat sheets. When a deck defines a term, quote the deck's wording exactly.

## Output

Markdown only, starting with the first heading. Use only `#`, `##`, `###`, bold, `- ` bullets, numbered lists, `> ` quotes and pipe tables. No HTML, no code fences. Cite the source of every definition, list and number as (deck name, slide N) or (reading, p. N). Mark tiers with **[NEW]**, **[EARLIER, UNTESTED]** or **[TESTED, Quiz k]**. Do not narrate the cases; include only what a question could ask.

Use exactly these sections, in this order:

# <Course> Quiz <N> Study Guide

## Scope and plan
Two or three sentences on what the quiz covers and why. Then one pipe table with three rows, **60 minutes**, **30 minutes** and **10 minutes**: what to do in that time, in order, and why. The student usually has about an hour: the hour should be read the glossary aloud, recite the lists from memory, take the practice quiz without looking back, then reread what was missed.

## Glossary
Every term the quiz could ask, in one place, alphabetical, one line each: **Term** (★ if among the fifteen most likely to be asked) — the definition in the professor's words — (deck, slide N) — tier tag. Include the new terms, the earlier untested terms and the tested ones; a question is nine times out of ten "what does this mean / which of these is it", so this table is what the student reads first and last. No commentary here; the detail lives in the next section.

## Key terms
The heart of the guide. Every term that is defined, bolded, titled on a slide or named in an assigned reading for the in-scope classes: be exhaustive; forty terms is normal. A pipe table with columns: Term | Definition in the professor's words (quoted, with slide) | In plain words | The tempting wrong answer. Order by likelihood of being asked.
Then **### Earlier terms still fair game**: a shorter table of terms from earlier classes, each tagged [EARLIER, UNTESTED] or [TESTED, Quiz k], one line each.

## Lists on the slides
Every enumerated list in the in-scope decks, verbatim, each under a bold title with its slide: principles, steps, tactics, characteristics, types, conditions. After each list give **Likely fakes:** two or three plausible items that are NOT on the list (the kind a "which is NOT" question would use), and say why each is wrong. Then the two or three earlier lists most likely to return.

## Confusion pairs
A pipe table: If the question asks for… | The answer | Not | The one-line test that separates them. At least eight rows, new material first.

## Exercises and their numbers
For each negotiation or exercise in scope: one line on what it was built to teach, the structure (parties, issues, scoring), and a small table of the figures and results the professor showed (class outcomes, benchmark values, payoffs). Only numbers that appeared on a slide or in the materials.

## Named studies and people
A pipe table: Study or person (year) | Setup | Result as the deck states it | The takeaway in five words. Include every citation on the in-scope slides.

## Past quiz questions
Every real past question you found on a "Quiz answers" slide, verbatim, with its answer and which deck it came from. If none are available, say so in one line. End with two sentences on what the pattern implies for this quiz.

## Practice quiz
Twenty-five to thirty multiple-choice questions in the real format: a one-sentence stem, four options labelled A to D, written in the professor's vocabulary. At least five must be "which of the following is NOT…" stems built from the lists above. At least twenty must come from the NEW tier. Order from most to least likely. After each question, on the next lines: **Answer:** the letter and the option; **Why not the others:** one clause per distractor; **Source:** deck and slide; **Likelihood:** High, Medium or Low.

## One-page card
What to reread in the last two minutes: the ten definitions most likely to be asked (one line each), the three lists most likely to be turned into a NOT question, and five numbers.

Quality checks before you finish: every term in Key terms also appears in the Glossary; every in-scope deck slide that defines or lists something is represented; no definition has been paraphrased into something a multiple-choice option could contradict; every practice question has exactly one defensible answer; nothing is included that could not be asked in a one-line multiple-choice question.

[CLASS-SPECIFIC NOTES]
