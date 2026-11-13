---
name: paperpush-pick-venue
description: >-
  Suggest where to submit a manuscript. Reads the manuscript yourself to choose
  its subjects, runs `paperpush pick-venue` (journals and conferences that
  publish similar work, from OpenAlex, with conference ranks, deadlines, and
  archival status, plus a ranking of the venues paperpush supports), then
  checks each candidate against the author's constraints (open access, APC
  budget, preprint policy, venues already tried) and, for supported venues,
  against the venue's manuscript rules (word, figure, and reference limits,
  article types). Reports a short list with the reasons for each, the
  citation metrics labelled for what they are, and which venues paperpush can
  submit to, then hands the chosen venue to paperpush-prepare-submission.
  Triggers: "where should I submit this paper", "pick a journal for my
  manuscript", "suggest venues for /path/to/paper", "which journals fit this
  manuscript", "which conference should I submit to", "paperpush-pick-venue".
---

# paperpush pick-venue (Claude skill)

`paperpush pick-venue` ranks venues on **subject alone**. It finds the
journals and conferences that published the works most similar to the
manuscript in OpenAlex, adds conferences that fit its subjects (most biology
and medicine meetings publish no proceedings, so OpenAlex can't see them), and
ranks the venues paperpush supports from their subjects. Conferences carry
their CORE/CCF rank, next deadlines, format (paper or abstract), length limit,
and whether they are archival. This skill adds what the command can't do:

- you read the manuscript and choose its subjects, rather than relying on a
  word list or an API key (`--ai`);
- you check each candidate against what the author needs and against the
  venue's rules, since a journal can match on subject and still be a poor
  choice (a Letters journal for a 9,000-word paper);
- you hand the chosen venue on to the next step.

You suggest venues; the author picks one. Never create a `.sub`, format, or
submit anything until they have chosen.

## Inputs

1. **The manuscript**: a file (`.tex`, `.zip`, `.docx`, `.pdf`, or a
   `.txt`/`.md` with the title and abstract) or a manuscript directory. If the
   user gave neither, ask for a path in plain text. Never guess it or scan
   the repository for one. A title and abstract pasted into the conversation
   also work (pass them as `--title` and `--abstract`).
2. **The author's constraints.** Ask once, in a single question, for any of
   these the user hasn't already given. Every one is optional; if they skip
   it, go on without it:
   - venue type: journal, conference, or either (`--venue-type`), or a
     preprint server (`--type preprint`);
   - for a conference: a full paper in archival proceedings, or an abstract,
     poster, or talk at a meeting that doesn't count as publication
     (`--archival archival|non-archival|all`; default archival only,
     `--non-archival` lists only meetings, `--archival all` both). Field norms differ: in computer
     science a conference paper *is* the publication; in biology and medicine
     a meeting abstract usually goes alongside a journal paper, not instead of
     one. If the author wants a meeting, suggest it in addition to journals;
   - for a conference: the latest deadline that works for them;
   - must the venue be open access, and is there an APC budget;
   - venues already tried or ruled out (a rejection, a conflict);
   - anything else that matters to them: a society, a field-specific
     audience, speed, a venue that requires or forbids a preprint.

   Also look in the manuscript directory, its parent, and any `<venue>/`
   folders for `*.sub.txt` files or `transfer.txt`. They show venues the
   manuscript was already prepared for or sent to. Treat those as "already
   tried" unless the user says otherwise, and mention that you did.

## Steps

1. **Read the manuscript.** Read the title, abstract, keywords, and enough of
   the introduction and results to know what the paper is: its field and
   subfield, the methods, the kind of contribution (new method, resource,
   biological finding, clinical study, review), and its length. Count the
   main-text words, display items (figures plus tables), and references
   roughly; you need them in step 4.

2. **Run the search.**

   ```
   paperpush pick-venue --manuscript <abs path> --limit 15 --json [--venue-type journal|conference] [--archival archival|non-archival|all]
   ```

   MCP: `pick_venue(manuscript=<abs path>, limit=15, venue_type=..., archival=...)`.

   This gives:

   - `venues`: the shortlist as one table, best first: journals,
     conferences, and preprint servers that published similar work
     (`basis: "similar works"`), plus conferences matched on subject alone
     (`basis: "subject"`, score at most 60). Each has `keywords`, `link`,
     `similar_works` and an `example`, `h_index`, `mean_citedness_2yr`,
     `is_oa`, `apc_usd`, `limit`, and a `paperpush_slug` when paperpush
     supports it; conferences also have `core_rank`, `ccf_rank`,
     `abstract_deadline`/`paper_deadline` (`deadline_estimated` when last
     cycle's dates were moved forward a year), `format`, and `archival`;
   - `openalex.journals`: the raw OpenAlex sources, with up to three
     `examples` each;
   - `matches`: the venues paperpush supports, ranked on their subjects.

   If `openalex.error` is set (no network, OpenAlex down), say so and work
   from `venues` (now the supported venues and subject-matched conferences)
   alone, and tell the user. If `conference_data.errors` names a feed,
   conference ranks or deadlines from it are missing: say so rather than
   reporting "no deadline".

   Read the `examples` of the top OpenAlex venues. If a venue's closest
   works are only loosely related (a shared keyword but a different field),
   drop it even if it ranks high. The OpenAlex score counts how much similar
   work a journal published, so big multidisciplinary journals and mega-journals
   rank high on volume. Keep them, but say that's why they're there.

3. **Correct the subjects.** Compare `subjects` in the result with
   `subject_vocabulary` (the subjects every supported venue is tagged with).
   If the subjects paperpush inferred miss what the paper covers, or include
   something it doesn't, run the command again with the 2–5 vocabulary
   entries you judge right, plus the manuscript's own keywords:
   `-k "<subject>,<subject>,<keyword>,..."` (MCP: `keywords=[...]`). A keyword
   that is itself a vocabulary entry matches venues directly, so this gives
   a better ranking of the supported venues.

4. **Check each candidate.** Take about 10 candidates: the best OpenAlex
   journals plus any supported `matches` they missed. For each one:

   - **Author's constraints**: drop venues that fail a hard constraint
     (not open access when that's required, APC over budget, already
     tried). `apc_usd` comes from OpenAlex/DOAJ and can be missing or out of
     date: say "APC not listed" rather than assuming there is none. Many
     subscription journals offer paid open access that OpenAlex doesn't
     record.
   - **Venue rules** (supported venues only): run
     `paperpush requirements <slug> --json` (MCP: `describe_venue`) and
     compare with what you counted in step 1: main-text word limit, abstract
     limit, display items, references, and whether the venue has an article
     type that fits the manuscript (`--article-type` shows that type's
     limits). Note each rule the manuscript breaks and by how much. Being
     over a limit is not disqualifying when the fix is a trim, but say so;
     a paper twice the limit, or with no matching article type, is a poor
     choice.
   - **Unsupported venues**: you have no rules to check beyond `limit` (for
     a conference, taken from its call for papers). Say "guidelines not
     checked" rather than guessing limits from memory.
   - **Conferences**: drop one whose next `paper_deadline` (or, for an
     abstract meeting, `abstract_deadline`) is past the author's date. An
     estimated deadline (`deadline_estimated`) is last year's moved forward:
     say "around <month>, not announced yet" and point to the `link`. Check
     `archival` against what the author wants, and for a double-blind
     computer-science conference mention that the manuscript must be
     anonymized.

5. **Report a short list** of 3–7 venues, best fit first. For each:

   - name, publisher, and venue type; for a conference, paper or abstract,
     archival or not, and the next deadline;
   - **why it fits**: one or two of the most similar works it published
     (from `examples`), in your own words;
   - open access / APC;
   - **citation metrics**: `h_index` and `mean_citedness_2yr`, labelled as
     *"2-year mean citedness (OpenAlex)"*, and for a conference its CORE
     rank (and CCF rank) when it has one. See
     [Citation metrics](#citation-metrics);
   - **rule issues** found in step 4, or "guidelines not checked";
   - **paperpush support**: `submit` (portal filled automatically),
     validate-only, or not supported.

   Then list, in one line each, the candidates you dropped and why (over the
   APC budget, already tried, off-topic examples), so the author can bring
   one back.

   Close with the caveat the command itself makes: these venues are ranked
   on subject fit and on the author's stated constraints, not on prestige or
   on the odds of acceptance.

6. **Hand off.** Ask which venue the author wants. Then:

   - a supported venue: run `paperpush-prepare-submission` for it with the
     same manuscript directory;
   - a venue paperpush doesn't support: `paperpush-prepare-submission` still
     works (it builds a temporary venue entry through
     `paperpush-add-venue-validate`), but it can only check the manuscript,
     not fill the portal;
   - the manuscript was already prepared for another venue (a `.sub` was
     found in the inputs step): offer `paperpush-transfer` instead, so
     the earlier files and values are reused.

## Citation metrics

Authors often want to know a journal's standing, and hiding it would only
send them to look it up elsewhere. Report the metrics, but don't let them
drive the ranking:

- **Never call any number an "impact factor".** The Journal Impact Factor
  is Clarivate's and paperpush doesn't have it. `mean_citedness_2yr` is
  OpenAlex's version of the same idea (mean citations in a year to the
  journal's works from the two years before). It's usually close to the JIF
  but not equal to it, so label it as OpenAlex's. If the user asks for the
  actual JIF, tell them to look it up in Journal Citation Reports or on the
  journal's site. Don't quote one from memory.
- **Don't rank or drop venues by these metrics** unless the author asks
  you to (e.g. "only journals with mean citedness above 5"). Then apply
  their threshold as a constraint and say you did.
- These are journal-level averages, skewed by a few highly cited papers,
  and not comparable across fields (a top mathematics journal has a lower
  number than a mid-tier cell biology one). Say this once in the report,
  not per venue.
- `h_index` grows with the journal's age and volume. It says more about how
  big a journal is than about how selective it is.
- **CORE and CCF ranks** (A*/A/B/C) are expert rankings of computer-science
  conferences only. A conference without one is not unranked in quality, just
  outside their scope (most biology and medicine meetings). Report them as
  "CORE A*", not as a measure of the paper's chances.
