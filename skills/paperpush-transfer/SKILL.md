---
name: paperpush-transfer
description: >-
  Transfer a manuscript that was submitted to one venue to another venue:
  copy the submission files into a new <venue>/ directory inside the
  submission's directory, create the new venue's .sub there, autofill it
  (reusing the values in any .sub files from earlier submissions), reformat
  the manuscript for the new venue (its Word/LaTeX template when one is found,
  else its layout rules), validate it, and write transfer.txt describing what
  changed. The original files are
  never modified. Triggers: "transfer my manuscript to <venue>", "resubmit
  this paper to <venue>", "my paper was rejected from <venue>, move it to
  <venue>", "reuse my nature sub for cell", "paperpush-transfer".
---

# paperpush transfer (Claude skill)

Moves a manuscript that was already prepared for (or submitted to) one venue
over to a new one. You copy the files, create and fill the new venue's `.sub`,
reformat the manuscript, validate it, and write `transfer.txt`, a plain-text
record of every change. The other skills and commands do the real work:
`paperpush-autofill` fills the `.sub`, `paperpush format` reformats the
manuscript copy for the new venue, `paperpush-validate` checks it, and `paperpush-fix` (only if the user agrees)
fixes what validate finds. This skill sequences them and carries values over
from the earlier submission.

The original submission is a record of what was sent. **Never edit, move, or
delete anything in the source directory**; every change happens in the copy.

## Inputs you need

1. **The source**: the directory of the earlier submission, or a set of files.
   Required. If the user did not give it, ask in plain text. Never guess it,
   scan the repository for candidate folders, or fall back to test fixtures.
2. **The target venue slug** (e.g. `cell`). Ask or infer it from their
   message, and confirm it with `paperpush venues`. If it is the same venue as
   the earlier `.sub`, this is not a transfer: say so, and point them to
   `paperpush-prepare-submission` or `paperpush-fix`.
3. **Earlier `.sub` files** (optional). Use every `.sub` / `.sub.txt` the user
   names, plus any in the source directory itself and in its transfer
   subdirectories (`<slug>/<slug>.sub.txt` from an earlier run of this skill).
   Read the `@venue:` line of each to learn which venue it was for. A
   `@venue: general` file (from `paperpush subfile general`) was never
   submitted anywhere; it is still a valid source, and its values carry over
   like any other's.

## Steps

1. **Resolve the source and the new directory.**
   - Source directory: the directory the user gave, or for a set of files, the
     closest directory that holds all of them.
   - New directory: `<source-dir>/<venue>/` (the slug, e.g. `cell/`).
   - If it already exists and is not empty, stop and ask: update it in place
     (keep its files and `.sub`, re-run steps 4-9) or stop. Never delete it.
   - If the source directory already holds transfer subdirectories (a
     subdirectory named after a venue slug with a `.sub` in it), ask which copy
     to start from: the original files, or a transfer that was already
     reformatted (e.g. `nature/` after `paperpush-fix` trimmed it).

2. **Copy the files** into the new directory, keeping their relative layout
   (`figures/fig1.png` stays `figures/fig1.png`), so LaTeX `\input` and
   `\includegraphics` paths still work:
   - For a directory, copy its whole tree except: other transfer
     subdirectories, `.sub`/`.sub.txt` files, `transfer.txt`, `.git/`,
     paperpush-fix backups, and LaTeX build output (`.aux`, `.log`, `.out`,
     `.synctex.gz`, `.fls`, `.fdb_latexmk`, `.bbl`, `.blg`). If the tree is
     over about 200 MB, list the largest items and ask before copying.
   - For a set of files, copy just those, plus any file an earlier `.sub`
     points to.
   - Use `cp -p` (or `rsync -a` with excludes) so timestamps survive. Record
     every file copied and every file left out, with the reason, for
     `transfer.txt`.

3. **Create the new `.sub`** with an absolute path:

   ```
   paperpush subfile <venue> -o <new-dir>/<venue>.sub.txt
   ```

   Then get the target schema (`paperpush schema <venue>`, hidden from
   `--help`) and its guidelines (`paperpush requirements <venue> --json`).

4. **Read the earlier `.sub` files** as data, with relative paths resolved:

   ```
   python -c "import json,sys; from paperpush.subfile import load; s=load(sys.argv[1]); print(json.dumps({'venue': s.venue, 'values': s.values}, indent=1))" <abs-path-to-old.sub>
   ```

   (or the `read_subfile` MCP tool). If several disagree on a field, prefer
   the most recently modified file and note the disagreement.

5. **Autofill the new `.sub`.** Invoke the `paperpush-autofill` skill (via the
   Skill tool) with the new `.sub` and the **new directory** as the
   manuscript directory, so every file path points at the copies. Follow it
   as written, with these additions when it builds `values.json`:

   - **Carry over** each earlier value whose field exists in the target
     schema, by the same `id`, or by an equivalent one (the same meaning under
     another name, e.g. `conflict_of_interest` -> `competing_interests`,
     `figures` -> `figure_files`; compare `label` and `help`). Set `source` to
     `"<old .sub filename>: <old field id>"`.
   - **Check carried text against the manuscript.** The manuscript may have
     been revised since the last submission. Where they differ, the
     manuscript wins; record the old and new value.
   - **Adapt, don't copy blindly:**
     - file and filelist paths: point them at the copies in the new directory
       (`<source-dir>/fig1.png` -> `<new-dir>/fig1.png`);
     - `authors`: re-order the columns into the target field's `help` format;
     - choice fields: keep a value only if it is in the target's `options`,
       else classify again from the manuscript;
     - venue-specific values (manuscript IDs, the old venue's article type,
       subject category, or section) are never carried; extract or classify
       them fresh for the new venue.
   - **Confidence:** `high` for a verbatim carry that matches the manuscript,
     `medium` for anything renamed, reformatted, or re-mapped.
   - **`never` fields stay unwritten**, as autofill requires, even when the
     earlier `.sub` has a value (suggested reviewers, license, consent).
     Put them in `unfilled` with the reason, and keep the earlier value for
     `transfer.txt` so the author can re-enter it if it still applies.
   - Write `values.json` in a temporary directory, not the new directory.

6. **Reformat the manuscript for the new venue.** Do this by default; skip it
   only when the user asked to keep the original formatting (record that in
   `transfer.txt`). It runs before validation so validation measures the
   reformatted file.
   - Only a Word (`.docx`) or LaTeX (`.tex`, or a `.zip` of LaTeX sources)
     manuscript can be reformatted. If the new `.sub`'s manuscript is a PDF,
     look in the new directory for its source (a `.docx` or `.tex` with the
     same stem, or the only one there) and pass it with `--manuscript`. If
     there is no source, skip the step and put "send the Word or LaTeX source
     to reformat it for <venue>" in the To-do list.
   - Run it with absolute paths, writing into the new directory and pointing
     the new `.sub` at the result:

     ```
     paperpush format <new-dir>/<venue>.sub.txt -o <new-dir>/formatted --update-sub --json
     ```

     (MCP: `format_manuscript(subfile=..., output_dir=..., update_subfile=true)`;
     add `--manuscript <abs source>` for the PDF case above, and without
     `--update-sub` set the `.sub`'s manuscript field to the formatted file
     yourself). It looks for the new venue's own template first (the recorded
     template link, then the venue's author-guideline pages) and otherwise
     applies the venue's layout rules. The unformatted copy stays in the new
     directory, so the author can go back to it.
   - When the manuscript is LaTeX and the PDF upload field points at a PDF,
     point it at the compiled `formatted/<stem>.pdf` (the JSON's `pdf`) when
     there is one; if the result did not compile, leave the field as it was
     and put the reason (from `left_to_author`) in the To-do list.
   - Keep the JSON result for `transfer.txt`: `template` (or `template_note`),
     `other_templates`, `changes`, `left_to_author`, and the `layout_checks`
     that did not pass. `left_to_author` (reference style, front matter the
     new class wants, figure placement, ...) goes into the To-do list too.
   - If `format` fails, report the error, keep the unformatted copy, and go on.

7. **Validate.** Invoke the `paperpush-validate` skill (via the Skill tool) on
   the new `.sub`, including the checks that need a reader. Do not run
   `paperpush-fix` yourself: if validate finds errors or warnings, ask whether
   to run it, and say that it edits only the copies in the new directory (the
   formatted manuscript, when step 6 made one). If they say yes, invoke it and
   use its final validation in step 8.

8. **Write `<new-dir>/transfer.txt`** (rewrite it if this is a re-run). Plain
   text, absolute paths, in this order:

   ```
   paperpush transfer: <old venue(s), or "unknown"> -> <new venue>
   Date: <YYYY-MM-DD>
   Source: <abs source dir>  (not modified)
   New directory: <abs new dir>
   New .sub: <abs path>
   Earlier .sub files used: <abs path> (<venue>, modified <date>), ...

   1. Files
      Copied: <n> files (<list, relative paths>)
      Not copied: <file> (<reason>), ...

   2. Fields
      Carried over unchanged: <id> (from <old .sub>), ...
      Carried over and adapted: <id>: <what changed and why>
          (e.g. "authors: columns re-ordered to Name | affiliation | email",
           "competing_interests: from conflict_of_interest")
      Updated from the manuscript: <id>: was "<old>", now "<new>"
      New for <new venue>, from the manuscript: <id>, ...
      Re-classified for <new venue>: <id>: <old value> -> <new value>
      Not carried over (no field in <new venue>): <id>, ...
      Left for you to fill: <id>: <reason> [earlier value: <value>]

   3. Guideline differences (<old venue> -> <new venue>)
      <rule>: <old> -> <new>   (word, abstract, figure, and reference limits,
      reference style, title page, file formats, anything else that differs)

   4. Formatting
      Formatted manuscript: <abs path>  (unformatted copy: <abs path>)
      Template: <URL or path>   or   No template (<template_note>); the
          venue's layout rules were applied
      Other templates found: <description>, ...
      Changes: <each change, one line each>
      Left for you: <each left_to_author item>
      Layout checks not passed: <id>: <message>, ...
      (or: "Not reformatted: <reason>" -- kept the original formatting at
       your request, no Word/LaTeX source, or the error format gave)

   5. Validation
      <n> errors, <n> warnings, <n> not checked
      <each error and warning, one line each>
      <what paperpush-fix changed, if it ran>

   6. To do
      <the author's remaining tasks, most important first>
      paperpush submit <abs new .sub>   (after: paperpush login <new venue>)
   ```

   Build section 3 from `paperpush requirements <slug> --json` for both
   venues, listing only the rules that differ. Skip it, with a line saying why,
   when no earlier venue is known or it has no requirements entry. Shorten
   long values (abstracts, statements) to their first 80 characters, with
   `...`, instead of pasting them whole.

9. **Report to the user.** Give the new directory, the `.sub`, and the path to
   `transfer.txt`, then a short summary: how many fields were carried over,
   updated, and new; the guideline differences that matter most (a lower word
   limit, a different reference style); whether the manuscript was reformatted
   and with which template (or the layout rules); the validation counts; and
   what is left for them. For the portal, point them to `paperpush-submit` (or
   `paperpush login <venue>` then `paperpush submit <abs .sub>`).

## Rules

- **Always use absolute paths** for the source, the new directory, the `.sub`
  files, and every path in `values.json`.
- Never modify, move, or delete anything outside the new directory.
- Never overwrite an existing transfer directory without asking.
- An earlier `.sub` is a source, not the truth: the manuscript wins when they
  disagree, and the change goes in `transfer.txt`.
- Never write a `never` field, even from an earlier `.sub`.
- Never invent values: if neither the earlier `.sub` nor the files give it,
  leave it for the author.
- Delegate filling to `paperpush-autofill`, reformatting to `paperpush format`,
  checking to `paperpush-validate`, and fixing to `paperpush-fix` (with the
  user's go-ahead); do not duplicate their logic here, and do not submit.
