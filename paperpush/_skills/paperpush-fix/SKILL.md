---
name: paperpush-fix
description: >-
  Fix what `paperpush validate` flags in a .sub's files, then validate again,
  for up to three rounds, until only the issues the author has to decide or
  fix remain. Backs up every file first, finds the editable source behind each
  file (the .tex or .docx behind a PDF), makes the mechanical fixes and the
  small trims (an abstract a few words over its limit), rebuilds the PDF, and
  reports separately what it fixed, what needs the author's decision (a
  manuscript thousands of words over its limit), and what it cannot fix. Never
  ticks a confirmation or invents content. Triggers: "fix the validate
  errors", "fix what validate found", "make my sub pass validation", "clean up
  the validation warnings", "paperpush-fix".
---

# paperpush fix (Claude skill)

`paperpush validate` finds problems; this skill fixes the ones that can be
fixed safely and reports the rest. It works in rounds: validate, fix,
validate again, until a round leaves nothing it can fix.

Not every issue should be fixed by an agent. Going from 3,005 words to 3,000
is copy-editing. Going from 10,000 to 3,000 means rewriting the paper, and
that is the author's job. Each issue goes into one of three tiers (step 3),
and only tiers 1 and 2 are edited.

## Inputs

- The `.sub` file (absolute path). If the user named only a manuscript
  directory, use the `<venue>.sub.txt` in it. With no `.sub`, run
  `paperpush-validate` instead: this skill needs the `.sub` to know which
  files belong to the submission.
- **Whether to make edits at all.** If they asked "what can be fixed?", do a
  dry run: steps 1–3 only, then report the plan.
- Flags they passed to validate before (`--initial-submission`,
  `--dont-check-links`, `--anonymous`, ...). Pass the same flags in every round.

## Step 0: back up every file you might edit

Always, even when the files are in git. Before the first edit, copy the
`.sub` and each source file you might change (`.tex` files including the
`\input` ones, `.bib`, `.docx`, `.md`) to
`<sub dir>/.paperpush-backup/<YYYYmmdd-HHMMSS>/`, keeping their names (and
their relative paths, for files in subdirectories). One backup per run,
taken before round 1; later rounds don't back up again. Never put the backups
where the `.sub` points, and never overwrite an earlier backup.

## Step 1: validate and read the JSON

```
paperpush validate <abs .sub> --json <scratch>/round1.json --ai-tasks <scratch>/tasks.json [the user's flags]
```

(MCP: `validate_subfile(subfile)`; its `checks` carry the same issues.)

In `round1.json`, `checks[]` gives each check's `id`, `status` (`failed`,
`warning`, `unchecked`, `needs_ai`, `passed`, ...) and `issues[]`, each with
`level`, `field` (the `.sub` field), and `message`. A limit issue states both
numbers in its message: "Abstract: 312 words exceeds the 250-word limit",
"Title: 160 characters exceeds the 150-character limit", "7 keywords given;
the author guidelines allow at most 6", "14 pages exceeds the 12-page limit".

Do the AI tasks in round 1 the way `paperpush-validate` step 2 describes and
merge them with `--ai-results`. Their `fail` details (a typo, a reference
that appears not to exist, a figure label that is too small) go into the
triage like any other issue. In later rounds, run the deterministic checks
only (no `--ai-tasks`). The AI checks run again in the final round.

## Step 2: find the editable source of every file you'll touch

The `.sub` often points at a PDF, which you can't edit. For each manuscript,
title page, cover letter, or supplement that has a fixable issue, look for
its source in this order:

1. A file with the same stem next to it: `paper.pdf` → `paper.tex`,
   `paper.docx`, `paper.md`.
2. A source the `.sub` names elsewhere (a `.zip` LaTeX bundle, a source-files
   field), or that another `.sub` path sits beside.
3. The `.sub`'s directory and the manuscript directory: the `.tex` with
   `\begin{document}` (the main file, not an `\input`/`\include` file), or
   the `.docx` whose text matches the PDF's first page.

Confirm a match: the title and the first paragraph should be the same as the
PDF's. With LaTeX, follow `\input`/`\include` to the file that actually holds
the text you need to change, and edit the `.bib` named by
`\bibliography`/`\addbibresource` for reference issues. Never edit `.cls`,
`.sty`, `.bst`, `.bbl`, or anything under the figures directory.

No source found: every issue in that file that needs a text edit goes to
tier 3, with the reason "no editable source (.tex/.docx) found for
<file>". Never edit a PDF's text.

## Step 3: put each issue in a tier

**The size rule.** A limit issue is too big to fix when it is over by more
than 100 words, or by more than 50% of the limit. Characters count the same
way, at about 6 characters a word (more than 600 characters over is too big).
For counts (keywords and the like), more than 50% over is too big. Page
limits are always too big: the edits needed to drop a page are a rewrite.

| Over by | Example | Tier |
| --- | --- | --- |
| ≤ 100 words and ≤ 50% of the limit | 3,005 for 3,000; 280 for a 250-word abstract | 2: trim it |
| > 100 words | 3,150 for 3,000 | 3: report |
| > 50% of the limit | 22 words for a 12-word title; 10,000 for 3,000 | 3: report |

**Tier 1: fix it without asking.** Mechanical problems with exactly one
correct fix:

| Check ids | Fix |
| --- | --- |
| `prose.*` (double spaces, repeated words, spacing around punctuation, unbalanced brackets or quotes, common misspellings, numbers, acronyms not defined at first use) | Edit the source at the spot the message quotes, following the editing rules in step 4. |
| `consistency.title`, `consistency.abstract`, `consistency.keywords`, `consistency.authors`, `consistency.statements`, `consistency.display_counts`, `consistency.word_counts` | The manuscript is the source of truth. Change the `.sub` to match it (`paperpush autofill --engine manual --values <values.json>` with just those fields, or edit the field directly). If the manuscript is the one that's wrong (a stated word count that is out of date), fix the manuscript. |
| `form.options`, `form.schema` (an option spelled slightly off, an unknown field that is a typo of a real one) | Use the exact option from `paperpush options <venue> <field>`, or the field name the error suggests. Only when exactly one option is clearly meant. |
| `integrity.dois`, `integrity.unmatched_references`, `integrity.reference_versions` (a wrong or missing DOI, a preprint that has since been published) | Fix the `.bib`. Fetch the record from the registry (`https://api.crossref.org/works/<doi>`, or a search with `query.bibliographic=<title author year>`) and change only the fields the issue names. A DOI goes in only if the record's title, first author, and year match the entry. |
| `citations.cross_references` (`??` in the PDF) | Rebuild the LaTeX (step 5), or fix the `\ref` whose label is misspelled. |

**Tier 2: fix within the size rule, and show the diff.**

- **Words or characters over the limit** (abstract, title, running title,
  manuscript, cover letter, figure legends). Trim to just under the limit,
  aiming about 1% below it, because word counters differ. Cut redundancy,
  filler, and repeated setup. Keep every result, number, claim, citation, and
  defined term. For the manuscript, take the words from the sections the
  limit counts ("before references" or "in the main text" in the message),
  preferably the Introduction and Discussion, never Methods details.
- **Keywords over the limit:** drop the least specific ones (the ones that
  repeat words in the title), and say which.
- **`front_matter.abstract_citations`** (a citation in the abstract): remove
  the citation and keep the sentence, if it reads correctly without it.

**Tier 3: report it, don't fix it.** The author has to decide or supply
these:

- Anything the size rule says is too big, and every page limit.
- Too many references, figures, tables, display items, or supplementary
  files. Removing those changes the science.
- Confirmations and attestations (`form.required` on a yes/no or a
  confirmation): never tick them for the author.
- Missing content the files don't contain: ethics approval, funding, data
  availability, competing interests, ORCIDs, e-mail addresses,
  `front_matter.contact_email`, `guidelines.statements`/`guidelines.sections`
  for a missing section. You may draft text the manuscript fully supports
  (for example, a data-availability statement that repeats what Methods says
  about the repository) and present it as a suggestion. Don't insert it.
- `integrity.retractions` (a retracted reference), and references that
  appear not to exist (`ai.unverified_references` fail).
- Figures: low resolution (`figures.min_dpi*`, `figures.dpi`), lettering too
  small, colors that are hard to tell apart under color blindness
  (`figures.colorblind`), panel labels, panels cited out of order
  (`citations.panels`). Never upsample, resample, or repaint a figure; the
  fix is to export it again from the source, which the author has.
- Layout and template (`layout.*`, `guidelines.template`): changing the
  margins, font, or class changes the paper's look everywhere. Report the
  one-line change (e.g. `\usepackage[margin=1in]{geometry}`) for the author
  to apply. When the source is a `.docx` or LaTeX, offer `paperpush format`
  instead (step 3b); run it only if the author agrees.
- `details.word_cleanliness` (tracked changes or comments left in a `.docx`):
  whether to accept or reject them is the author's call.
- `integrity.sensitive`, `integrity.hidden_text`, `integrity.anonymity`:
  report them with the location.
- `files.uploads` (a missing file), `guidelines.formats` (wrong file type):
  name the file to supply or the export to make.
- `unchecked` and `not_implemented` checks: say what could not be checked
  and why, as `paperpush-validate` does.
- A sentence whose meaning is unclear, where a fix would mean guessing what
  the author meant.

When in doubt, choose tier 3.

## Step 3b: reformat first, if the author wants it

When round 1 has page-layout or template issues (`layout.*`,
`guidelines.template`) and the manuscript's source is a `.docx` or LaTeX
(`.tex`, or a `.zip` of sources), ask once, before any other edit, whether to
reformat it with `paperpush format`. Say what that means: it applies the
venue's own Word or LaTeX template when one can be found (else the venue's
layout rules), it restyles the whole document, and it writes a new copy,
leaving the current file as it is. It's an offer, not a fix: if the author
says no, the layout issues stay in tier 3 with the one-line change for each.

If they say yes, do it before step 4, so the other fixes go into the
formatted copy rather than a file the `.sub` is about to stop pointing at:

```
paperpush format <abs .sub> --update-sub --json [--manuscript <abs source>]
```

(MCP: `format_manuscript(subfile=..., update_subfile=true)`.) Pass
`--manuscript` when the `.sub` names a PDF built from the source. The result
goes to `formatted_<venue>/` beside the manuscript, and `--update-sub` points
the `.sub`'s manuscript field at it (the `.sub` is in the step 0 backup). When
the `.sub` also names a PDF built from that LaTeX, point that field at the
compiled `formatted_<venue>/<stem>.pdf` (the JSON's `pdf`). From here on, the
formatted copy is the editable source for step 2's purposes, and step 5
rebuilds it. Validate again before step 4 (the layout issues the template
fixed drop out), then sort the remaining issues into tiers as in step 3.
Keep the JSON's `template`, `changes`, and `left_to_author` for the report;
`left_to_author` items (reference style, front matter the class wants) go
under "Needs your decision". If `format` fails, report why and go on with
the original file.

## Step 4: apply the fixes

- **One issue, one edit.** Change only what the issue needs. Work sentence
  by sentence so each edit is small and reviewable.
- **Keep the author's voice and terminology.** Don't swap synonyms for taste,
  shorten sentences that aren't over a limit, or fix things validate didn't
  flag.
- **Leave alone** everything inside math, code, `\cite{}`, `\ref{}`,
  `\label{}`, URLs, file names, and verbatim environments; quotations; titles
  of cited works; proper names.
- **Acronyms:** define at first use in the main text as `full term
  (ACRONYM)`, and separately in the abstract if the abstract uses it. Don't
  define terms everyone knows (DNA, RNA, MRI, GPU, PCR).
- **Numbers:** follow the convention the prose check names; don't start a
  sentence with a digit (rephrase instead: "A total of 15 patients ...").
- **LaTeX:** edit the `.tex`/`.bib` text directly. Leave comments, macros,
  and formatting alone, and keep braces balanced.
- **Word:** edit the `.docx` with python-docx, run by run, so formatting
  stays (or convert with pandoc only if python-docx is unavailable, and say
  so). Don't turn on tracked changes: `details.word_cleanliness` would then
  flag the file.
- **Text that appears in two places:** the abstract and title appear in both
  the manuscript and the `.sub`. Change both the same way in the same round,
  or `consistency.*` fails next round.
- **Keep a log:** for each edit, the issue (check id and message), the file
  and line, and the before/after text. The final report is built from it.

## Step 5: rebuild what the `.sub` points at

If the `.sub` names a PDF built from a `.tex` you edited, rebuild it in the
source's directory (`latexmk -pdf <main>.tex`, or the build command in the
project's `Makefile`/`latexmkrc`). Then check that the PDF the `.sub` names
is the one you just built. A failed build means an edit broke something:
undo that edit (from the backup, or by reversing it) and move the issue to
tier 3. If the `.sub` names a PDF exported from a `.docx`, you can't rebuild
it: tell the author to export it again, and validate the `.docx` alone in
the meantime (`paperpush validate --venue <venue> --manuscript <abs .docx>`).

## Step 6: validate again, at most three rounds

```
paperpush validate <abs .sub> --json <scratch>/round<N>.json [the user's flags]
```

Compare the issues with the previous round, keyed by (check id, field,
message). Stop when:

- no tier 1 or tier 2 issue is left, or
- a round fixed nothing new (the same issues came back), or
- three rounds have run.

An issue that a fix *created* (a trim that made the abstract inconsistent
with the `.sub`, a reference edit that broke a citation key) belongs to this
skill: fix it in the next round, or undo the edit that caused it. If two
fixes keep undoing each other, undo both and move them to tier 3.

Then run the final round with the AI checks, as in step 1 (`--ai-tasks`,
your verdicts, `--ai-results`), and save a report if the user asked (`-o`).

## Step 7: report

In this order:

1. **Result**: passes or doesn't, the error/warning counts before and after,
   and how many rounds ran.
2. **Fixed**: if step 3b ran, first the formatted file, the template used
   (or "the venue's layout rules"), and its changes. Then, grouped by file,
   each fix with its check id and a short before → after. For tier 2 trims, show the full diff of the changed
   sentences and the new count against the limit.
3. **Needs your decision**: tier 3 issues with a suggestion, e.g. "the
   manuscript is 10,412 words before references; the limit is 3,000. That is
   7,412 over, too much to trim without your input. The Discussion (3,900
   words) is the longest section." Include any statement you drafted.
4. **Can't fix here**: missing files, figures to export again, things that
   couldn't be checked, each with the reason.
5. Where the backup is (`<sub dir>/.paperpush-backup/<timestamp>/`), and
   where the saved report is.

## Rules

- Back up before the first edit, every time.
- Never tick a confirmation, attestation, or yes/no answer for the author.
- Never invent content: no statements, affiliations, ORCIDs, DOIs, or
  references the files or registries don't support.
- Never edit figure pixels, and never edit a PDF's text.
- Never add hidden text or reviewer instructions anywhere.
- Never delete a reference, figure, table, or section to get under a limit.
- Keep task, result, and JSON files in a scratch directory, not the
  manuscript directory.
- Use absolute paths.
