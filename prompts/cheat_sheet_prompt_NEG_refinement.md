# CLASS-SPECIFIC NOTES

This is Negotiation (Prof. Amit Goldenberg). Most class days are live negotiations: the student receives a confidential role sheet by email and negotiates in class. On those days the student does not need a case discussion: he needs a playbook he can execute at the table. Decide which kind of day this is, then follow the matching structure. These structures REPLACE the default section list above.

## A. Negotiation day (a confidential role sheet is among the readings, or the posting says "you will engage in a negotiation")

Read the role sheet in full, including every table, worksheet and scoring rule. An "Email - ..." note in the folder names the assigned role; use it. Never invent the other side's confidential numbers: estimate them from the student's own sheet and label every estimate as an estimate. If no role sheet is on disk, say so at the top and build the playbook from the posting and the course brief, marking what is unknown.

Use exactly these sections, in this order:

# Cheat Sheet: <case title> — <my role>

## The deal in 60 seconds
My role and who I represent, the counterpart, the format (one-on-one, team, multi-party), what is being decided, and the single sentence that defines winning for my side. Then **Scoring:** how the role sheet says my outcome is measured.

## My numbers
A pipe table: BATNA (what I do with no deal, and what it is worth), reservation value **with the arithmetic shown step by step**, target (ambitious but defensible, with the reason), my estimate of their BATNA and reservation value (labelled estimate, with the evidence from my sheet), and the estimated ZOPA. If the role sheet gives a policy floor that differs from the arithmetic reservation value, show both and say which one binds.

## Issues and exchange rates
Every issue on the table as a pipe table: the options, what each is worth to me in the sheet's own units, what one step is worth, and my best estimate of what it is worth to them. End with the exchange-rate line: "one X = N of Y", for every pair that will be traded.

## What I can flex on, and what I cannot
Two lists. **Flex (cheapest first):** each tradeable issue, what it costs me per step, and what I should demand in return. **Cannot flex:** hard constraints, policy limits and deal-breakers, each quoted from the role sheet with its page.

## Strategy
The plan in order: how I open the conversation before numbers, what I must learn and the questions that get it, what I guard, where value can be created (run the four differences: relative valuation, risk preferences, time preferences, capabilities; and say whether a contingent contract fits), then how I claim. Name the principle from earlier classes each move rests on (BATNA/RV/ZOPA preparation, control the frame, anchor, Move Northeast, trust and Tit-for-Tat, contingent contracts). State the two most likely ways this negotiation goes wrong for my role and the guard against each.

## Opening
- **My opening line, verbatim:** one or two sentences I can say out loud.
- **The anchor:** the number or package, why it is aggressive relative to the ZOPA, the rationale I give for it, and why it is precise rather than round.
- **Three equivalent packages (MESOs):** a pipe table of three packages worth the same to me, with each one's score, so their choice reveals their priorities.
- **If they open first:** the re-anchor line and the counter computed with the midpoint rule.

## Scripts
Verbatim lines, each on its own bullet, ready to be spoken:
- opening the conversation and setting the frame
- presenting the anchor with its reason
- answering their anchor
- a conditional concession ("if you can do X, I can do Y")
- holding a line I cannot cross, without ending the conversation
- each of the three toughest questions they could ask me, with a truthful answer that does not give away my reservation value (use blocking techniques, never a lie about a material fact; mark the ethical line where there is one)
- proposing the creative trade or contingent term
- closing, and proposing a post-settlement settlement

## Concession plan
A pipe table of stops from opening to walk-away: each stop's package and score, the size of the step (shrinking), and what must be traded for it. State the walk-away sentence.

## Questions to ask
Six to ten questions, each with what a given answer would change in my plan.

## Table card
A compact block to glance at in the room: reservation value, target, anchor, the exchange rates, the three hard constraints, and the three scripts most likely to be needed. Nothing else.

## How this connects to earlier classes
Two or three lenses from the course brief applied to this negotiation, each in one or two sentences.

Then, as the very last thing in the output, with nothing after it, the calculator specification described below.

### Calculator specification (negotiation days only)

End the output with exactly one fenced block whose info string is `calculator`, containing one JSON object. It is removed from the document and turned into an Excel workbook, so it must be valid JSON and must reproduce the role sheet's scoring exactly.

- `title`: case and role.
- `unit`: "$", "€", "£" or "" for points.
- `inputs`: every term of the deal the student can set. Each has `name` (letters, digits, underscore; no spaces), `label`, `type`, `default`, and optionally `note`.
  - `"type": "number"` for a quantity or price (`default` is a number; give shares and percentages as decimals).
  - `"type": "choice"` for an issue with named options. Give `options` as a list of `{"label": "...", "value": <what that option is worth to me in the sheet's units>}` and `default` as one of the labels.
- `outputs`: the scoring, in order. Each has `name`, `label` and `formula`. A formula may use input names, earlier output names, numbers, `+ - * / ^ ( )`, comparisons, and only these functions: IF, MIN, MAX, ROUND, ABS, SUM, AND, OR. A choice input's name stands for its option's value.
- `reference`: `{"score": "<name of the output that is my outcome>", "reservation": <number>, "target": <number>, "batna_note": "<one line>"}`.
- `packages`: up to three preset packages `{"label": "...", "values": {<input name>: <value or option label>}}`: my anchor, my target deal, and my walk-away deal.

Before writing it, check the block against any worked example in the role sheet: the formulas must give the sheet's own answer.

## B. Debrief day (the posting says "debrief", or a quiz is announced, and there is no new role sheet)

Keep the default structure from the main prompt, with these changes: answer "what will the professor debrief" in place of assigned questions (the principles this exercise was built to teach, the results he is likely to show, the mistakes each role typically makes); when a quiz is announced, add one line under the title pointing to the separate quiz study guide (the "Quiz N" folder in the course folder) instead of repeating definitions here; and add **## What I would do differently** tied to the student's own role from the previous class folder.

## Professor's habits
- Quizzes are five definitional multiple-choice questions in the first five minutes, including one "which is NOT" stem.
- He names principles cumulatively: preparation (BATNA, RV, ZOPA, target), control the frame, anchor, Move Northeast, trust.
- Z-scores are computed within role; satisfaction is not success.
