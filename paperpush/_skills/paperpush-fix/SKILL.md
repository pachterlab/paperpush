---
name: paperpush-fix
description: >-
  Fix what `paperpush validate` flags in a .sub's files, then validate again,
  for up to three rounds, until only the issues the author has to decide or
  fix remain. Backs up every file first, finds the editable source behind each
  file (the .tex or .docx behind a PDF), makes the mechanical fixes and the
  small trims (an abstract a few words over its limit), rebuilds the PDF,
  applies arxiv_latex_cleaner to LaTeX source after reviewing its output, and
  reports separately what it fixed, what needs the author's decision (a
  manuscript thousands of words over its limit), and what it cannot fix. Never
  ticks a confirmation or invents content. Triggers: "fix the validate
  errors", "fix what validate found", "make my sub pass validation", "clean up
  the validation warnings", "clean my LaTeX source", "paperpush-fix".
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
- An earlier validation result, if the user gave one (a `--json` file or a
  saved report) or this conversation already ran one. Step 1a decides
  whether to reuse it.
- **Whether to make edits at all.** If they asked "what can be fixed?", do a
  dry run: steps 1–3 only, then report the plan.
- Flags they passed to validate before (`--initial-submission`,
  `--disable-integrity.links`, `--anonymous`, ...). Pass the same flags in every round.

## Step 0: back up every file you might edit

Always, even when the files are in git. Before the first edit, copy the
`.sub` and each source file you might change (`.tex` files including the
`\input` ones, `.bib`, `.docx`, `.md`, and any `.zip`/`.tar` LaTeX bundle) to
`<sub dir>/.paperpush-backup/<YYYYmmdd-HHMMSS>/`, keeping their names (and
their relative paths, for files in subdirectories). One backup per run,
taken before round 1; later rounds don't back up again. Never put the backups
where the `.sub` points, and never overwrite an earlier backup.

## Step 1: get round 1's validation results

### 1a: reuse an earlier run if there is one and it is still current

Validation is slow, and the AI checks are the slowest part. Don't redo a run
that is already done. Look for one in this order:

1. **A file the user gave you**: a `--json` report, or a saved report
   (`.txt`, `.md`, `.html`, `.pdf` from `-o`), with any `--ai-results` file
   that went with it.
2. **A run earlier in this conversation** on this `.sub` (for example
   `paperpush-validate` just ran): its `--json` file and AI verdicts.

Every report starts with how it was made. The JSON's first keys are
`command` (the `paperpush validate ...` command as typed), `cwd` (where it
ran), `generated` (when, ISO 8601), and `inputs` (the `.sub` and each upload
it names, each with `path`, `size`, `modified`, and `sha256`). A saved report
has `Command:` and `Generated:` lines under its title. (MCP:
`validate_subfile` returns `generated` and `inputs`, not `command`.)

Use the earlier run as round 1 only if all of these hold:

- **Same submission.** It was run on this `.sub` (the `.sub` path in
  `command`/`inputs`, or the report's `File:` line).
- **Nothing has changed since.** For a JSON report, compute the SHA-256 of
  each file in `inputs` now (`shasum -a 256 <file>`); every one must match,
  and no file is missing. Also check the sources behind them that step 2
  would edit (a `.tex`, its `\input` files, the `.bib`, the `.docx` behind
  a PDF): none may be modified after `generated`. A saved report has no
  hashes, so compare the modification time of the `.sub`, each file it
  names, and those sources against its `Generated:` line.
- **The flags are known.** Take them from `command`, and drop its output
  options (`--json`, `-o`, `--ai-tasks`, `--ai-results`, `--overwrite`,
  display options such as `--show-errors-only`). If the user now asks for
  different flags, run validation again.
- **The details are there.** You need each issue's check id, field, and
  message. A JSON report has them. A saved report with `--show-errors-only`,
  `--hide-warnings`, or `--hide-unchecked` left some out: run validation
  again.

When a condition fails, say which (for example "manuscript.tex changed
after the report was generated") and run validation fresh (1b). A report
with no `Command:`/`Generated:` header came from an older paperpush. You
can't tell whether it is current, so run it again.

When the earlier run is still current, tell the user you're reusing it and
from when. Use its checks as round 1. If it already has AI verdicts (its
`ai` says "agent results from ..."), use them. If its AI checks are still
`needs_ai`, do them now (as below), with
`paperpush validate <abs .sub> [the flags] --ai-only --ai-tasks <scratch>/tasks.json`,
which skips the deterministic checks.

### 1b: otherwise, validate

```
paperpush validate <abs .sub> --json <scratch>/round1.json --ai-tasks <scratch>/tasks.json [the user's flags]
```

(MCP: `validate_subfile(subfile)`; its `checks` carry the same issues.)

### Reading the results

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
  `front_matter.contact_email`, `front_matter.keywords`, `guidelines.statements`/`guidelines.sections`
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
  report them with the location. (LaTeX comments and junk files in a bundle
  are the exception: step 5b removes them.)
- `files.uploads` (a missing file): name the file to supply.
- `guidelines.formats` (wrong file type): when the file is a `.docx`, `.tex`,
  or `.md` and the venue accepts one of `docx` or `tex`, offer
  `paperpush convert` (step 3c). Otherwise name the export to make.
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

## Step 3c: convert first, if the venue needs another file type

When round 1 has `guidelines.formats` because the venue wants Word and the
source is LaTeX or Markdown, or wants LaTeX and the source is Word or
Markdown, ask once, alongside the step 3b question, whether to convert it. Say
that it writes a new copy and leaves the current file as it is. If they say
no, the issue stays in tier 3.

If they say yes, convert before step 3b, so `format` works on the new file and
the other fixes go into it:

```
paperpush convert <abs .sub> --to docx|tex --no-source --update-sub --json [--manuscript <abs source>]
```

(MCP: `convert_manuscript(subfile=..., to=..., embed_source=false, update_subfile=true)`.)
`--no-source` keeps the original LaTeX out of a `.docx` the venue will receive:
it can hold text a reader cannot see, such as `\todo` notes or, on a double-blind
submission, author names. Pass `--bib` if the `.bib` is not next to the source.
Then validate again.

Read the JSON. `clean: false` means something did not come through: `counts`
shows each item in the source and in the output, and `warnings` says what to
check. Each warning goes under "Needs your decision". Never report a
conversion as done without relaying them. If `convert` fails, report why and
leave the issue in tier 3.

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

## Step 5b: apply arxiv_latex_cleaner, and check what it did

`validate` only previews the cleaner, in the `integrity.latex_cleaner` check.
This step is what applies it. Do it once per run, after the other edits.

**When.** Look at `integrity.latex_cleaner` in round 1:

- `warning` with issues (arXiv and venues that submit through it, where the
  source goes public): apply it without asking.
- `passed` with a `detail` saying what it "would change" (any other venue):
  ask once, alongside the step 3b question, before any edit. Say that it
  strips the comments from the working `.tex` files, with the copies kept in
  the backup. If the author says no, skip this step and say so in the report.
- `passed` with "would change nothing", or `not_applicable`: skip this step.
- `unchecked`: the package is missing, or the cleaner failed. Tell the author
  to `pip install 'paperpush[validate]'`, or give the reason, and list it
  under "Can't fix here".

In a dry run, report what the check says and stop.

**The tool makes mistakes, so review every change.** It works by pattern
matching. On its own it deleted a figure whose path came from a macro and cut
`keep % this` in a `verbatim` block to `keep %`. You are the check on it.

1. Copy the source to `<scratch>/latex-clean/source/`: the whole LaTeX project
   directory (without `.git` or `.paperpush-backup`), or the unpacked bundle.
   Never run the cleaner on the author's directory.
2. From `<scratch>/latex-clean/`, run:

   ```
   arxiv_latex_cleaner <abs scratch>/latex-clean/source --keep_bib
   ```

   It writes `source_arXiv/` beside `source/`. Without `--keep_bib` it deletes
   the `.bib` files.
3. Run `diff -ru source source_arXiv` and read all of it. Check:
   - **Every removed file.** It must really be unused. Search the remaining
     `.tex`, `.sty`, and `.cls` files for its name and stem, and look for what
     pattern matching misses: paths built by macros, `\graphicspath`,
     `\input`/`\subimport` with macros, `\foreach` loops over file names,
     `\lstinputlisting`/`\verbatiminput`, pgfplots data files, and `.bbl`,
     `.bst`, `.cls`, `.sty` files the build reads. Keep a PDF or figure the
     `.sub` names.
   - **Every changed line.** The only change allowed is removing a comment or
     a commented-out block. Restore the original line if the cleaner touched
     a `%` in `verbatim`, `lstlisting`, `minted`, `\verb`, or a URL (`%20`),
     an escaped `\%`, or any text that was not a comment. Restore a block it
     removed that was live code. A trailing `%` that suppresses a line break
     must stay.
4. Repair `source_arXiv/`: copy back each wrongly removed file and restore
   each wrongly changed line. Log each repair with its reason.
5. Compile a throwaway copy of the repaired `source_arXiv/` with `latexmk
   -pdf`, and compile a copy of `source/` the same way. The page counts must
   match, and `pdftotext` output from the two PDFs must be the same. If they
   differ, find the change that caused it and restore it, and if you can't,
   skip the cleaner and move it to tier 3. With no LaTeX compiler, say in the
   report that the build was not confirmed.
6. Apply it. For loose `.tex` uploads, copy each changed `.tex` file (the
   uploads and the files they `\input`) over its original and delete nothing
   in the author's directory: other files there may not belong to the paper.
   For a bundle, repack the repaired `source_arXiv/` to the same path, in the
   same format, with the same top-level layout. The `.sub` doesn't change.

**Afterwards.** The next validate may still list lines you restored on
purpose, because the cleaner would change them again. That's expected: don't
re-run this step for them, and name them in the report. Comments the cleaner
missed, or visible notes such as `\todo{...}`, go under "Needs your decision"
with the location. Don't delete them yourself.

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
2. **Fixed**: if step 3c ran, first the converted file and its `counts`
   and `warnings`. If step 3b ran, then the formatted file, the template used
   (or "the venue's layout rules"), and its changes. Then, grouped by file,
   each fix with its check id and a short before → after. For tier 2 trims, show the full diff of the changed
   sentences and the new count against the limit. If step 5b ran, say how
   many comment lines and files the cleaner removed, what you restored and
   why, whether the build was confirmed, and that the original comments now
   exist only in the backup.
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
- Never run arxiv_latex_cleaner on the author's files directly, and never apply
  its output without reviewing every change (step 5b).
- Never delete a reference, figure, table, or section to get under a limit.
- Keep task, result, and JSON files in a scratch directory, not the
  manuscript directory.
- Use absolute paths.
