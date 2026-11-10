``paperpush validate``
======================

Run the pre-submission checks on a filled ``.sub`` file. Run this before
:doc:`submit` to catch problems while they are still cheap to fix. It can also
check manuscript files without a ``.sub``, with or without a venue (see
:ref:`validate-files`).

.. code-block:: text

   usage: paperpush validate [-h] [-v] [-q] [--disable-CHECK ...] [--enable-CHECK ...] [--anonymous]
                             [--initial-submission]
                             [-o [OUT]] [--overwrite] [--cover-letter-max-age DAYS]
                             [--show-errors-only] [--hide-warnings]
                             [--hide-unchecked] [--colorblind]
                             [--ai] [--provider {anthropic,openai,google}]
                             [--model MODEL] [--ai-only] [--json [FILE]]
                             [--ai-tasks FILE] [--ai-results FILE] [--venue VENUE]
                             [--manuscript FILE] [--title-page FILE]
                             [--cover-letter FILE] [--figures FILE [FILE ...]]
                             [--tables FILE [FILE ...]]
                             [--supplement FILE [FILE ...]]
                             [--publisher PUBLISHER] [--issn ISSN]
                             [--journal TITLE]
                             [subfile]

Synopsis
--------

.. code-block:: bash

   paperpush validate biorxiv.sub.txt
   paperpush validate biorxiv.sub.txt -o              # also save biorxiv_validation.pdf
   paperpush validate biorxiv.sub.txt --ai            # also run the AI checks via an API
   paperpush validate biorxiv.sub.txt --disable-prose # turn off any check, or family of checks
   paperpush validate nature.sub.txt --initial-submission  # errors become warnings
   paperpush validate --manuscript main.tex --figures fig1.png fig2.png   # no .sub, no venue
   paperpush validate --venue nature --manuscript main.pdf --cover-letter cover.docx
   paperpush validate --issn 0012-365X --manuscript main.pdf   # a journal paperpush does not list

``validate`` prints a report of every check, grouped and colored by what
happened to it: **failed** (red), **warnings** (amber), **needs AI**
(purple), **not checked** -- because it could not run here, with the reason
on the line below, or because paperpush has no check for the rule yet (gray)
-- and **passed** (green). Errors must be fixed before the file can be submitted
(for example, a required field left blank, a referenced file that does not
exist, or a choice value that isn't one of the venue's allowed options);
warnings are worth reviewing but don't block submission. See
:ref:`how findings are reported <vc-report>` and :doc:`../validation-checks`.

The command exits non-zero if any errors are found, so it fits cleanly into
scripts and CI.

.. _validate-files:

Checking files without a ``.sub``
---------------------------------

Name the files with ``--manuscript``, ``--title-page``, ``--cover-letter``,
``--figures``, ``--tables``, and ``--supplement`` (the last three take several
files). What runs depends on what else is given:

``.sub`` and file flags
   Every check, as usual. Each flag replaces the path the ``.sub`` gives for
   that file, which is handy for checking a new draft without editing the
   ``.sub``. ``--venue``, if given, must match the ``.sub``'s ``@venue:`` line;
   a ``.sub`` with no such line is checked against ``--venue``, or without it
   the general checks.

``--venue VENUE`` and file flags, no ``.sub``
   The files are checked against the venue's author guidelines: formats,
   length, sections, title-page items, layout, figures, and the rest. The
   submission-form checks, the ``.sub``-versus-manuscript comparison, and the
   OpenReview lookup are reported as not applicable, since there are no form
   fields to read. The report says so at the top; it is not a verdict on
   whether the submission is ready.

File flags only
   Only the checks that apply to any manuscript run: reachable links,
   reference DOIs and registry searches (fabricated or mismatched
   references), retractions, secrets and private data, hidden text, the order
   figures and tables are cited in, citations in the abstract and a contact
   e-mail, every reference cited, and the prose checks. Add ``--anonymous`` to
   check for identifying information as well. ``paperpush pick-venue`` suggests
   venues to check against next.

Each file goes in the venue's upload field for it. If the venue's form has no
such upload -- a separate title page for a portal that takes only the
manuscript, say -- the file is still checked, and the report notes that the
portal has nowhere to upload it. Venues whose guidelines ask for a separate
title page (``title_page.separate_file``) warn when ``--title-page`` is not
given.

.. _validate-publisher:

Journals paperpush does not list: publisher house rules
--------------------------------------------------------

``--publisher PUBLISHER`` (a slug or name, e.g. ``elsevier`` or
``"Springer Nature"``), ``--issn ISSN``, or ``--journal TITLE`` check the
named files, or a ``general`` ``.sub``, against the house rules of the
journal's publisher (see :doc:`publishers`). ``--issn`` and ``--journal`` ask
Crossref's journal registry who publishes the journal (network access needed;
``--journal`` needs the exact title, so ``--issn`` is more reliable). They also
name the journal, so the cover letter is checked for it; ``--publisher`` alone
leaves that check out. An ISSN or title that turns out to be a journal
paperpush lists is checked against that venue's own guidelines. When the
publisher has no house rules recorded, only the general checks run.

Everything a house rule catches is a warning, marked "house rule", never an
error: a journal can depart from its publisher's rules, so read the journal's
own guidelines before acting on one. The same holds for a listed venue: where
its own guidelines are silent and its publisher's house rules state a rule,
that rule applies as a warning. These options are refused for a ``.sub`` made
for a listed venue.

Relative paths are resolved against the working directory. ``-o`` with no path
saves the report next to the first file named. ``--web`` needs a ``.sub``.

What it checks
--------------

This is a summary, grouped as in ``paperpush validate --help``.
:doc:`../validation-checks` lists every check in detail, with its severity and
how to turn it off.

Checks that run without AI
^^^^^^^^^^^^^^^^^^^^^^^^^^

**Bibliography** (:ref:`vc-bibliography`)

- Every reference cited; every citation in the reference list
- Self-citations within limits (at most 15, and at most 80% of a list of 5 or
  more)
- BibTeX entries complete, closed, and with unique keys

**Figure and table citations** (:ref:`vc-citations`)

- Main, supplementary, and Extended Data figures and tables first cited in
  order
- Every one cited
- None cited that is missing

**Submission and manuscript consistency** (:ref:`vc-consistency`; compared word for word)

- Title
- Abstract
- Authors (names, order, e-mails, ORCID iDs)
- Keywords
- Statements

**Cover letter** (:ref:`vc-cover-letter`)

- Addressed to the target venue
- Dated within the last 7 days

**Additional manuscript details** (:ref:`vc-placeholders`, :ref:`vc-bibliography`)

- No placeholders, to-do notes, comments, or highlighting left in the files
- Word files have no tracked changes or EndNote fields
- Affiliation numbers after author names are superscripted
- Reference list is alphabetical or follows first citation

**Figures** (:ref:`vc-figures`; the attached figure files, or the figures
embedded in the manuscript when none are attached)

- Minimum resolution (dpi, with a separate line-art threshold)
- Lettering within the size range and in the required typeface, in vector
  figures
- Panels labeled in the required style (case, bold) and every cited panel
  present
- Color mode (RGB, CMYK, grayscale) matches the guidelines
- Within the guidelines' maximum width and height
- Placed where the guidelines ask
- Legends within the length limit and placed where the guidelines ask

**Files** (:ref:`vc-files`, :ref:`vc-length`)

- Files exist
- Accepted types and sizes
- Combined upload size within the portal's limit
- Manuscript within the portal's word and page limits

**Submission form** (:ref:`vc-fields`, :ref:`vc-choices`)

- Required fields and confirmations
- Valid options, including nested drill-down fields
- Field types
- Number of entries in list fields (keywords, authors, suggested reviewers, ...)
  within the form's limits
- Word and character limits
- URLs
- Author list (parses, with exactly one corresponding author)

**Abstract and front matter** (:ref:`vc-front-matter`)

- No citations in the abstract (the ``.sub``'s and the manuscript's)
- Structured-abstract headings, when the venue asks for them
- A contact e-mail address in the manuscript

**Venue guidelines** (:ref:`vc-guidelines`, measured against
``manuscript_requirements.json``; see :doc:`requirements`)

- Manuscript format and size
- Word and page limits, for the article type selected in the ``.sub``
- Title, abstract, and keyword limits
- Undefined abbreviations in the abstract
- Required sections and their order
- Required statements (data availability, competing interests, ...)
- Title-page items
- Separate title page
- Reference count within the limit
- Citation style matches the guidelines (numbered or author-year; numbered in
  order)
- DOIs on every reference
- Preprints and unpublished work
- Number of figures, tables, and supplementary files within the guidelines'
  limits
- Combined supplementary PDF
- Cover letter
- Page layout matches a compiled copy of the venue's template
  (:ref:`vc-template`)

**Integrity and external sources**

- Reachable links (:ref:`vc-links`)
- Reference DOIs point at the right paper (:ref:`vc-references`)
- References without a DOI match a registered work; preprints that have been
  published, and old arXiv versions (:ref:`vc-reference-search`)
- Secrets and private data, and what ``arxiv_latex_cleaner`` would strip from
  LaTeX source (:ref:`vc-sensitive`)
- Hidden text and prompt injection (:ref:`vc-hidden-text`)
- Anonymity, for double-blind venues (:ref:`vc-anonymity`)
- Each author has an active OpenReview profile matching their name and email
  domain, for OpenReview venues (:ref:`vc-openreview`):
  every author has an Open Review ID (a warning for each one that doesn't,
  even if their name matches a single profile); the ID exists and its
  profile is active (errors); the name in the ``.sub`` matches the profile;
  and the profile lists a current position, carries the author's email
  suffixes, and has DBLP and expertise for reciprocal reviewers. Needs the
  login stored by ``paperpush login VENUE``.

**Page layout** (:ref:`vc-layout`)

- Uses the venue's Word template or LaTeX class (detected from the file)
- Page size and margins match the guidelines
- Body font and text size match the guidelines
- Text is double-spaced
- Lines numbered continuously; pages numbered
- Spelling follows the required English variant (US or UK)

**Prose** (:ref:`vc-prose`; a mechanical proofread, not a grammar check)

- No double spaces between words (after a sentence is fine)
- No accidentally doubled words ("the the")
- Spacing around punctuation ("word ,", "word,word")
- Brackets and quotes pair up
- Common misspellings (codespell)
- Varied word choice (no stock words, overused words, a word repeated within
  a sentence, runs of sentences opening the same way)
- Numbers of 10 or more in digits; no sentence opening with digits
- Abbreviations written one way: "i.e."/"e.g.", "et al.", "vs.", "Fig."/"Figure",
  hyphenated or closed compounds
- Acronyms spelled out once, at first use (in the abstract and again in the
  main text)

**Rigor and reproducibility** (:ref:`vc-rigor`)

- Cell lines authenticated, tested for mycoplasma, and not known to be
  misidentified
- Blinding, randomization, sample size, exclusions, and sex reported for animal
  and human studies
- Antibodies, cell lines, and organisms carry RRIDs that resolve
- Reported test statistics agree with their p values (statcheck)
- A clinical trial gives a trial registry ID

Checks that require AI
^^^^^^^^^^^^^^^^^^^^^^

These run with ``--ai``, or through an agent with ``--ai-tasks`` /
``--ai-results``; otherwise they are listed as not checked (:ref:`vc-ai`).

- Lettering size, typeface, and panel labels in raster figures
- Title-page items found only by reading (postal address, author footnotes,
  one-sentence summary, classification, affiliations)
- Tables are editable text, not pasted images
- Reference list follows the guidelines' style
- Looks prepared with the venue's template, for a PDF or Word manuscript
- Follows the guidelines' other free-text rules (those not checked
  automatically)
- Whether every ``.sub`` field agrees with the manuscript in meaning
- Spelling, grammar, and tense in the prose

Guideline rules no check reads are listed as **not implemented**.

The link, reference, sensitive-information, manuscript, hidden-text, and
OpenReview passes make network requests or read every uploaded file; each can
be turned off, like any other check, with ``--disable-<CHECK>`` (see
:ref:`vc-disable`).

Arguments
---------

``subfile``
   Path to the ``.sub`` file to check, e.g. ``biorxiv.sub.txt``. Optional when
   files are named with the flags below (see :ref:`validate-files`).

``--venue VENUE``
   Check the named files, or a ``.sub`` with no ``@venue:`` line, against this
   venue's author guidelines. Without it, only the general checks run.

``--manuscript FILE``, ``--title-page FILE``, ``--cover-letter FILE``
   The manuscript, a separate title page, and the cover letter. Each replaces
   the ``.sub``'s path for that file, or with no ``.sub`` is checked on its
   own.

``--figures FILE ...``, ``--tables FILE ...``, ``--supplement FILE ...``
   Figure, table, and supplementary files, as above.

``--web``
   Instead of printing the report, open the ``.sub`` in the paperpush web page
   on this computer and run the checks there. You can fix fields and
   re-validate, and every edit is saved to the file. With ``--ai`` the AI checks
   run too. ``--reference-check-extended`` and ``--reproduce-figures`` are not
   available with ``--web``.
   Needs ``pip install 'paperpush[web]'``.

``--disable-<CHECK>``
   Turn a check off; repeat for more. ``<CHECK>`` is a check ID from
   :ref:`the full list <vc-check-list>`, such as
   ``--disable-ai.consistency`` or ``--disable-bibliography.age``, or the
   start of one, for that whole family: ``--disable-prose``,
   ``--disable-ai.notes``. ``--disable-references`` turns off every reference
   lookup, and ``--disable-manuscript`` everything that reads the manuscript
   against the guidelines, along with compiling LaTeX and the AI checks. A
   disabled check is listed as not checked, never as passed. See
   :ref:`vc-disable` for the rules, and for what the older ``--dont-check-*``
   flags became. ``<CHECK>`` can also be comma-separated names
   (``--disable-prose,ai.notes``) or a text file of names
   (``--disable-checks.txt``).

``--enable-<CHECK>``
   Run only these checks; the rest are listed as not checked. ``<CHECK>`` is
   given as for ``--disable-<CHECK>``. See :ref:`vc-enable`.

``--anonymous``
   Check the submission for information that identifies its authors, as for a
   double-blind venue. Venues marked ``anonymous`` in ``venues.json`` (ICLR,
   AAAI) get this check automatically. Linked anonymous.4open.science
   repositories are fetched and scanned too, which needs network access. See
   :ref:`vc-anonymity`.

``--initial-submission``
   Validate the file as an initial submission: every error is reported as a
   warning, so nothing blocks it, and the command exits 0. Meant for venues
   whose guidelines let the initial submission be in any format (Nature and
   its sister journals, the Cell journals, PLOS Computational Biology, PNAS,
   Bioinformatics, Science Advances, ...; recorded as
   ``manuscript.initial_submission_any_format``). For those venues the report
   opens with a "Format for initial submission" note quoting the rule, with or
   without this flag. Used on a venue without such a rule it still turns
   errors into warnings, and the note says the venue states no such rule.
   ``paperpush submit`` still validates normally. On the web page, the
   "initial submission (any format)" checkbox next to the Validate buttons
   does the same (it is greyed out for venues without the rule).

``--cover-letter-max-age DAYS``
   Warn when the date at the top of the cover letter is more than DAYS days
   old (default 7) or lies in the future. See :ref:`vc-cover-letter`.

``-o [OUT]``, ``--output [OUT]``
   Also save the report. PDF by default, which keeps the colors; a path ending
   in ``.html``, ``.md``, or ``.txt`` saves that format instead. With no OUT,
   writes ``<subfile>_validation.pdf`` next to the ``.sub``. Refuses to replace
   an existing file (checked before any checks run) unless ``--overwrite`` is
   given.

``--overwrite``
   Replace the report file named by ``-o`` if it already exists. (``--force``
   is accepted as an older name.)

``--show-errors-only``
   Show only the failed checks and their errors. Overrides
   ``--hide-warnings`` and ``--hide-unchecked``.

``--hide-warnings``
   Leave out the *Warnings* section, and the warnings listed under failed
   checks.

``--hide-unchecked``
   Leave out the checks that were not run: the *could not run*, *needs AI*,
   and *not implemented* sections.

   These three display flags filter both the terminal output and the report
   saved with ``-o``. They do not apply with ``--web``. The summary line still
   counts every check, a ``Not shown:`` line says which sections were left
   out, and the exit status is the same.

``--colorblind``
   Use color-blind-friendly colors (blue for passed, vermillion for failed,
   dark yellow for warnings, reddish purple for needs AI, gray for could not
   run / not implemented) in the terminal and the saved report. The web app's
   *Colorblind-friendly colors* setting uses the same palette, and with
   ``--web`` the page opens with that setting on. ``--alt-color`` is an older
   name for this flag.

``--ai``
   Also run the checks that need a reader's judgment through an LLM API, with
   the manuscript, figures, and other files attached. Uses whichever of
   ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, or ``GEMINI_API_KEY`` is set and
   the provider's SDK (``pip install "paperpush[ai]"``). If the request
   fails, the report says why and lists those checks as needing AI.

``--provider {anthropic,openai,google}``, ``--model MODEL``
   With ``--ai``: the provider and model to use (defaults as for
   :doc:`autofill`).

``--json [FILE]``
   Print the report as JSON instead of the coloured report (the closing notes
   go to stderr, so stdout is only the JSON), or with ``FILE`` also write the
   JSON there. Each check has ``id``, ``title``, ``category``, ``status``, and
   ``issues``; each issue has ``level``, ``field``, and ``message``. The
   report starts with how it was made: ``command`` (the command as typed),
   ``cwd``, ``generated`` (when it ran), and ``inputs`` (the ``.sub`` and
   each upload, with ``path``, ``size``, ``modified``, and ``sha256``), so
   you can tell later whether the files have changed since. Reports saved
   with ``-o`` have ``Command:`` and ``Generated:`` lines under the title.
   The ``paperpush-fix`` skill reads it, and reuses it as its first round
   when nothing has changed.

``--ai-tasks FILE``
   Write the AI checks to ``FILE`` as JSON -- each with an id, instructions,
   and the files to read -- for an agent such as Claude Code or Codex to do
   with its own subscription. The ``paperpush-validate`` Claude skill drives
   this.

``--ai-results FILE``
   Merge an agent's verdicts on the AI checks into the report instead of
   calling an API. ``FILE`` holds ``{"results": [{"id", "status", "details",
   "evidence"}]}`` with ``status`` one of ``pass``, ``fail``, ``unclear``,
   ``not_applicable``.

``--ai-only``
   Report only the checks that need AI, without the deterministic ones (form
   fields, files, links, DOIs, sensitive data, hidden text, OpenReview
   profiles, and the manuscript's measured checks). The manuscript is still
   read, offline, to find which AI checks apply. Combine it with ``--ai``,
   ``--ai-tasks``, or ``--ai-results``; on its own it lists the AI checks as
   needing AI. The report carries no ready-to-submit verdict, and it cannot be
   combined with ``--disable-manuscript`` or ``--web``.

``--disable-<CHECK>``, ``--anonymous``, ``--initial-submission``,
``--cover-letter-max-age``, ``--references-alphabetical``, and
``--references-citation-order`` work on :doc:`submit` too.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags. Use ``-v``
to see the checks as they run.

Examples
--------

.. code-block:: bash

   paperpush validate biorxiv.sub.txt

Fail a CI job if the submission isn't ready:

.. code-block:: bash

   paperpush validate biorxiv.sub.txt || exit 1

Run the field and file checks alone, with no network access:

.. code-block:: bash

   paperpush validate biorxiv.sub.txt --disable-integrity.links --disable-references

Skip the prose checks and one check that does not apply to your paper:

.. code-block:: bash

   paperpush validate biorxiv.sub.txt --disable-prose --disable-bibliography.age

List only what must be fixed:

.. code-block:: bash

   paperpush validate biorxiv.sub.txt --show-errors-only

Save a color-blind-friendly HTML report:

.. code-block:: bash

   paperpush validate biorxiv.sub.txt -o report.html --colorblind

Let an agent do the AI checks, then merge its answers:

.. code-block:: bash

   paperpush validate biorxiv.sub.txt --ai-tasks tasks.json
   # ... the agent writes results.json ...
   paperpush validate biorxiv.sub.txt --ai-results results.json -o

Re-run just the AI checks, e.g. after fixing a figure:

.. code-block:: bash

   paperpush validate biorxiv.sub.txt --ai-only --ai-results results.json

See also
--------

- :doc:`../validation-checks` — every check in detail.
- :doc:`options` — look up the valid values for a field flagged as invalid.
- :doc:`requirements` — print the author-guideline rules the manuscript is
  measured against.
- :doc:`submit` — the next step once validation passes.
