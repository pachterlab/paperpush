---
name: paperpush-respond-to-reviewers
description: >-
  Prepare a revised manuscript and a response to reviewers from the reviews
  (editor and reviewer comments) of a submitted manuscript. Runs in two
  phases. Phase 1 makes a revisionN/ directory next to the manuscript, copies
  the manuscript files and the reviews into it, drafts the response document
  with every comment in place, and proposes a plan for each point. Phase 2,
  after the author approves or edits the plan, revises the manuscript copy,
  writes the responses, builds the marked manuscript (changes in red text by
  default, or highlight / Word Track Changes / none, as the venue or the
  author says), and runs paperpush-validate. The original files are never
  modified. Triggers: "respond to the reviewers", "write a response to
  reviewers", "address the reviewer comments", "prepare my revision",
  "rebuttal letter", "point-by-point response", "paperpush-respond-to-reviewers".
---

# paperpush respond to reviewers (Claude skill)

Turns a decision letter into a revision: a clean revised manuscript, a marked
manuscript showing the changes, and a filled-in response to reviewers. It runs
in **two phases with a stop between them**: you set things up and propose a
plan, the author approves or changes it, and only then do you edit the
manuscript.

The submitted files are a record of what the reviewers read. **Never edit,
move, or delete anything outside the new `revisionN/` directory.**

## Inputs you need

1. **The manuscript**: its directory, or its files (manuscript, figures,
   supplements, cover letter, `.sub`). Required. If the user did not give it,
   ask in plain text. Never guess it, scan the repository for candidates, or
   fall back to test fixtures.
2. **The reviews**: the decision letter or review document(s) (`.pdf`,
   `.docx`, `.txt`, `.md`, `.eml`, or text pasted into the chat). Required; ask
   if missing. Pasted text: save it as `reviews.txt` in the new directory.
3. **The venue slug** (optional but useful): from the user, else the
   `venue:` line of a `.sub` in the manuscript directory, else the journal
   named in the decision letter (confirm it with `paperpush venues`). If none
   is known, say the venue defaults below apply.
4. **Overrides the user states** (optional), which beat the venue's rules:
   - how to mark the changes: "red text", "highlight", "track changes",
     "no marked copy", or something else they describe;
   - the revision number, if they want a specific one;
   - anything about the response format (a venue template, a word limit).

Use absolute paths throughout.

## Phase 1: set up and plan (no edits to the manuscript yet)

### 1. Find the base directory and the revision number

- **Base directory**: the manuscript directory the user gave, or, for a set
  of files, the closest directory that holds all of them.
- If the input is itself a revision directory (its name matches
  `revision<N>`, e.g. `revision1/`), this is a further round: the base is its
  parent, and you start from the files of the **latest** revision (the clean
  revised manuscript, not the marked copy), not from the original. Say so.
- **Revision number X**: the user's number if they gave one; otherwise one
  more than the highest `revision<N>` directory already in the base
  directory (`revision1` if there is none). New directory:
  `<base>/revisionX/`.
- If `<base>/revisionX/` exists and is not empty, stop and ask: continue in
  it (keep its files, resume at the first step not done) or stop. Never
  delete or overwrite it.

### 2. Copy the files

Copy into `<base>/revisionX/`, keeping the relative layout
(`figures/fig1.png` stays `figures/fig1.png`) so LaTeX `\input` and
`\includegraphics` paths and relative `.sub` paths still work:

- For a directory: its whole tree except other `revision<N>/` directories,
  transfer directories made by `paperpush-transfer` (a subdirectory named
  after a venue slug holding a `.sub`), `.git/`, paperpush-fix backups, and
  LaTeX build output (`.aux`, `.log`, `.out`, `.synctex.gz`, `.fls`,
  `.fdb_latexmk`, `.bbl`, `.blg`). Starting from an earlier revision, also
  leave out its marked manuscript and its response document. If the tree is
  over about 200 MB, list the largest items and ask before copying.
- For a set of files: just those, plus any file a `.sub` among them names.
- Copy the reviews into `<base>/revisionX/reviews/`.
- Use `cp -p` (or `rsync -a` with excludes). Keep the list of what you copied
  and left out, for the summary.

Keep one untouched copy of the main manuscript source to diff against later:
`<base>/revisionX/.original/<manuscript file>` (and its `\input` files for
LaTeX). Every manuscript edit in phase 2 goes to the main copy, never to
`.original/`.

### 3. Work out the marked-manuscript format

In this order, the first that applies wins:

1. what the user said (step "Overrides");
2. the venue's rule: `paperpush requirements <venue> --json`, key
   `revision.marked_manuscript`, plus any `revision.notes` (read those too:
   a response template, line numbers, a page limit);
3. the default: **`red_text`**.

| Value | Marked manuscript |
| --- | --- |
| `red_text` | added and rewritten text in red; deleted text left out |
| `highlight` | added and rewritten text highlighted (yellow); deleted text left out |
| `track_changes` | a Word file with Track Changes (insertions and deletions as revisions) |
| `none` | no marked manuscript |
| `other` | as described in `revision.notes` or by the user |

`track_changes` needs a Word manuscript. For a LaTeX or Markdown manuscript,
say so in the plan and propose `red_text` instead (or a Word export if the
author wants one).

### 4. Read the reviews and draft the response document

Read the reviews and the manuscript in full. Split the reviews into single
points, **verbatim** (never paraphrase or shorten a reviewer's words), and
number them: `E1, E2, ...` for the editor, `R1.1, R1.2, ...` for Reviewer 1,
and so on. A paragraph that raises two issues becomes two points.

Write the draft response document into `<base>/revisionX/` in the
manuscript's own format: `response_to_reviewers.docx` for a Word
manuscript, `response_to_reviewers.tex` (and its PDF) for LaTeX, else
`response_to_reviewers.md` (and a PDF or `.docx` via pandoc if the venue wants
one). Use a venue template if the venue or the author names one. Layout:

- a short opening letter to the editor (thanks, a one-paragraph summary of
  the main changes) - drafted in phase 2;
- a heading per reviewer (`Editor`, `Reviewer 1`, ...);
- for each point: its number, then **the comment in italics**, then the
  author's response in **plain (non-italic) text** below it. Quote changed
  manuscript text in the response in quotation marks, with where it is in
  the revised manuscript (section, and page/line when the manuscript has line
  numbers).

In phase 1 the responses are a one-line placeholder: `[Response to come:
<planned action>]`.

### 5. Write the plan and stop

Write `<base>/revisionX/revision_plan.md` and show the plan in the chat. For
each point:

- **Point** number and a one-line summary of what is asked.
- **Type**: text change, clarification, new analysis or experiment,
  figure/table change, reference addition, or disagreement (respectfully
  argued, with the reason).
- **Proposed change**: what you will change, and where (section, paragraph,
  figure).
- **Needs from the author**: anything you cannot do or must not make up -
  new data, new results, a new figure, numbers, a decision between options.

Then the overall items: the marked-manuscript format and why (user / venue /
default), the files you will produce (step 9), length limits that matter
(from `paperpush requirements`), and the revision directory.

**Stop here.** Ask the author to approve the plan, change it point by point,
or supply what you marked as needed from them. Do not edit the manuscript
until they answer.

## Phase 2: revise (after the author approves)

Apply the plan as the author left it. If they changed a point, follow their
version; if they gave new results or text, use it as given.

### 6. Revise the manuscript

Edit the manuscript copy in `<base>/revisionX/` (keep its file name, so the
`.sub` and `\input` paths still hold). Change only what the plan says. Keep
the author's voice and the venue's limits. **Never invent data, results,
statistics, or references.** Where a change needs something the author has
not supplied, put a visible placeholder (`[AUTHOR: add the n for the
validation cohort]`) and list it in the summary. Figures you cannot redo:
leave the file and note it. For a LaTeX manuscript, make sure it still
compiles.

### 7. Build the marked manuscript (unless the format is `none`)

Compare the revised manuscript with `.original/` and mark every addition and
rewrite. Name it `<stem>_marked.<ext>` beside the clean copy, plus its PDF
when the source is not a PDF.

- **LaTeX**: `latexdiff .original/<main>.tex <main>.tex > <main>_marked.tex`
  (`--flatten` when the manuscript uses `\input`). Then, in the marked file,
  right after the line `%DIF END PREAMBLE EXTENSION ADDED BY LATEXDIFF`, add
  for `red_text`:

  ```
  \usepackage{xcolor}
  \renewcommand{\DIFadd}[1]{{\color{red}#1}}\renewcommand{\DIFdel}[1]{}
  \renewcommand{\DIFaddFL}[1]{{\color{red}#1}}\renewcommand{\DIFdelFL}[1]{}
  ```

  and for `highlight` the same with `\usepackage{soul}` and `\hl{#1}` in
  place of `{\color{red}#1}`. Compile it with `latexmk -pdf`. If latexdiff is
  not installed, tell the author how to install it and mark the text by hand
  with `\textcolor{red}{...}` instead.
- **Word**: use the `docx` skill if available. For `red_text` or
  `highlight`, copy the revised `.docx` and set the font color to red (or
  apply yellow highlighting) on every run that is new or changed compared
  with `.original/` (compare paragraph by paragraph, word by word). For
  `track_changes`, write the differences as real Word revisions
  (`w:ins` / `w:del`, author "Authors"), so the editor can accept or reject
  each one.
- **Markdown / other text**: wrap added text in
  `<span style="color:red">...</span>` (or `<mark>` for highlight) and render
  it with pandoc.

Check the marked file: every change the response cites is marked, and
nothing unchanged is.

### 8. Write the responses

Fill in each placeholder in the response document: plain text, polite and
direct, saying what was changed and where (quote the new text with its
section and line), or, for a point the authors decline, why. Write the
opening letter to the editor. Keep comments verbatim and italic. Leave a
`[AUTHOR: ...]` placeholder where the response needs something from them.

### 9. Validate

Get a `.sub` for the revision in `<base>/revisionX/`:

- an earlier `.sub` was copied: rewrite any absolute path in it that points
  into the base directory so it points at the copy in `revisionX/`
  (relative paths already do); its manuscript field must name the **clean**
  revised manuscript, not the marked copy or the response;
- no `.sub` and the venue is known: `paperpush subfile <venue> -o
  <base>/revisionX/<venue>.sub.txt`, then the `paperpush-autofill` skill
  with `revisionX/` as the manuscript directory;
- no venue known: skip validation and say why.

Then invoke the `paperpush-validate` skill (via the Skill tool) on it,
including the checks that need a reader. Report errors and warnings; offer
`paperpush-fix` (it edits only the copies in `revisionX/`) but do not run it
unasked. Validation checks the revised manuscript against the venue's
guidelines; it does not judge the responses.

### 10. Report

Give the author, with absolute paths:

- the revision directory;
- **always**: the response to reviewers and the clean revised manuscript;
- the marked manuscript and its format (or "not produced: <reason>");
- the validation counts and the main issues;
- every `[AUTHOR: ...]` placeholder left, by file and point, most important
  first;
- that the revision is uploaded through the venue's **"submit revision" link
  for the existing manuscript**, not with `paperpush submit`, which is for
  initial submissions and would open a second submission.

## Rules

- Two phases: never edit the manuscript before the author approves the plan.
- Never modify, move, or delete anything outside `revisionX/`; never
  overwrite an existing revision directory without asking.
- Reviewer and editor comments are quoted verbatim, in italics; responses are
  plain text.
- Never invent data, results, or references; ask, or leave a visible
  `[AUTHOR: ...]` placeholder.
- The user's choice of marking beats the venue's, which beats the default
  (red text).
- Do not submit the revision, and do not run `paperpush submit` on it.
- Delegate checking to `paperpush-validate`, filling a new `.sub` to
  `paperpush-autofill`, and fixing to `paperpush-fix` (with the author's
  go-ahead).
