``paperpush validate``
======================

Run the pre-submission checks on a filled ``.sub`` file. Run this before
:doc:`submit` to catch problems while they are still cheap to fix. It can also
check manuscript files without a ``.sub``, with or without a venue (see
:ref:`validate-files`).

.. code-block:: text

   usage: paperpush validate [-h] [-v] [-q] [--dont-check-links]
                             [--dont-check-for-sensitive-info]
                             [--dont-check-references]
                             [--dont-check-manuscript] [--anonymous]
                             [--dont-check-hidden-text]
                             [--dont-check-openreview-profiles]
                             [--arxiv-latex-cleaner] [--initial-submission]
                             [-o [OUT]] [--overwrite] [--cover-letter-max-age DAYS]
                             [--show-errors-only] [--hide-warnings]
                             [--hide-unchecked] [--colorblind]
                             [--ai] [--provider {anthropic,openai,google}]
                             [--model MODEL] [--ai-only] [--ai-tasks FILE]
                             [--ai-results FILE] [--venue VENUE]
                             [--manuscript FILE] [--title-page FILE]
                             [--cover-letter FILE] [--figures FILE [FILE ...]]
                             [--tables FILE [FILE ...]]
                             [--supplement FILE [FILE ...]]
                             [subfile]

Synopsis
--------

.. code-block:: bash

   paperpush validate biorxiv.sub.txt
   paperpush validate biorxiv.sub.txt -o              # also save biorxiv_validation.pdf
   paperpush validate biorxiv.sub.txt --ai            # also run the AI checks via an API
   paperpush validate nature.sub.txt --initial-submission  # errors become warnings
   paperpush validate --manuscript main.tex --figures fig1.png fig2.png   # no .sub, no venue
   paperpush validate --venue nature --manuscript main.pdf --cover-letter cover.docx

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
   ``.sub``. ``--venue``, if given, must match the ``.sub``'s venue.

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

Relative paths are resolved against the working directory. ``-o`` with no path
saves the report next to the first file named. ``--web`` needs a ``.sub``.

What it checks
--------------

This is a summary, grouped as in ``paperpush validate --help``.
:doc:`../validation-checks` lists every check in detail, with its severity and
the flag that controls it.

Checks that run without AI
^^^^^^^^^^^^^^^^^^^^^^^^^^

**Submission form** (:ref:`vc-fields`, :ref:`vc-choices`)

- Required fields and confirmations
- Valid options, including nested drill-down fields
- Field types
- Item counts
- Word and character limits
- URLs
- Author list (parses, with exactly one corresponding author)

**Files** (:ref:`vc-files`, :ref:`vc-length`)

- Files exist
- Accepted types and sizes
- Total upload size
- Portal word and page limits

**Integrity**

- Reachable links (:ref:`vc-links`)
- Reference DOIs point at the right paper (:ref:`vc-references`)
- References without a DOI match a registered work; preprints that have been
  published, and old arXiv versions (:ref:`vc-reference-search`)
- Secrets and private data (:ref:`vc-sensitive`)
- Hidden text and prompt injection (:ref:`vc-hidden-text`)
- Anonymity, for double-blind venues (:ref:`vc-anonymity`)
- OpenReview IDs and profiles, for OpenReview venues (:ref:`vc-openreview`):
  every author has an Open Review ID (a warning for each one that doesn't,
  even if their name matches a single profile); the ID exists and its
  profile is active (errors); the name in the ``.sub`` matches the profile;
  and the profile lists a current position, carries the author's email
  suffixes, and has DBLP and expertise for reciprocal reviewers. Needs the
  login stored by ``paperpush login VENUE``.

**Guidelines** (:ref:`vc-guidelines`, measured against
``manuscript_requirements.json``; see :doc:`requirements`)

- Manuscript format and size
- Word and page limits, for the article type selected in the ``.sub``
- Title, abstract, and keyword limits
- Undefined abbreviations in the abstract
- Required sections and their order
- Required statements (data availability, competing interests, ...)
- Title-page items
- Separate title page
- Reference count
- Citation style (numbered or author-year; numbered in order)
- DOIs on every reference
- Preprints and unpublished work
- Figure, table, and supplementary counts
- Combined supplementary PDF
- Cover letter
- Edits to a mandatory template (:ref:`vc-template`)

**Layout** (:ref:`vc-layout`)

- Template or LaTeX class
- Page size and margins
- Body font and size
- Double spacing
- Line numbers and page numbers
- English spelling variant

**Figures** (:ref:`vc-figures`; the attached figure files, or the figures
embedded in the manuscript when none are attached)

- Resolution (dpi, line-art threshold)
- Lettering size and typeface in vector figures
- Panel labels (case, bold, cited panels present)
- Color mode
- Print size
- Placement
- Legend length and location

**Citation order** (:ref:`vc-citations`)

- Main, supplementary, and Extended Data figures and tables first cited in
  order
- Every one cited
- None cited that is missing

**.sub vs paper** (:ref:`vc-consistency`; compared word for word)

- Title
- Abstract
- Authors (names, order, e-mails, ORCID iDs)
- Keywords
- Statements

**Abstract and front matter** (:ref:`vc-front-matter`)

- No citations in the abstract (the ``.sub``'s and the manuscript's)
- Structured-abstract headings, when the venue asks for them
- A contact e-mail address in the manuscript

**Reference list** (:ref:`vc-bibliography`)

- Every reference cited; every citation in the reference list
- Self-citations not excessive (more than 15, or more than 80% of the list)
- BibTeX entries complete, closed, and with unique keys

**Prose** (:ref:`vc-prose`; a mechanical proofread, not a grammar check)

- Double spaces between words (not after a sentence)
- Repeated words ("the the")
- Spacing around punctuation ("word ,", "word,word")
- Unbalanced brackets and quotes
- Common misspellings (codespell)
- Repetitive word choice (stock words, overused words, a word repeated within
  a sentence, runs of sentences opening the same way)
- Numbers of 10 or more in digits; no sentence opening with digits
- Abbreviations written one way: "i.e."/"e.g.", "et al.", "vs.", "Fig."/"Figure",
  hyphenated or closed compounds
- Acronyms spelled out once, at first use (in the abstract and again in the
  main text)

Checks that need AI
^^^^^^^^^^^^^^^^^^^

These run with ``--ai``, or through an agent with ``--ai-tasks`` /
``--ai-results``; otherwise they are listed as not checked (:ref:`vc-ai`).

- Lettering size, typeface, and panel labels in raster figures
- Title-page items found only by reading (postal address, author footnotes,
  one-sentence summary, classification, affiliations)
- Tables pasted as images
- Reference formatting in the venue's style
- Template used, for a PDF or Word manuscript
- The venue's free-text guideline rules
- Whether every ``.sub`` field agrees with the manuscript in meaning
- Spelling, grammar, and tense in the prose

Guideline rules no check reads are listed as **not implemented**.

The link, reference, sensitive-information, manuscript, hidden-text, and
OpenReview passes make network requests or read every uploaded file; each can
be turned off with its ``--dont-check-*`` flag below.

Arguments
---------

``subfile``
   Path to the ``.sub`` file to check, e.g. ``biorxiv.sub.txt``. Optional when
   files are named with the flags below (see :ref:`validate-files`).

``--venue VENUE``
   With no ``.sub``: check the named files against this venue's author
   guidelines. Without it, only the general checks run.

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
   run too. Needs ``pip install 'paperpush[web]'``.

``--dont-check-links``
   Skip probing the URLs cited in the uploaded files. Avoids network requests.

``--dont-check-references``
   Skip resolving the references' DOIs through ``doi.org``, and the registry
   searches for references without a DOI and for newer versions (Crossref,
   OpenAlex, arXiv, bioRxiv). Avoids network requests.

``--dont-check-for-sensitive-info``
   Skip scanning the uploaded files for information not meant to be published.

``--dont-check-manuscript``
   Skip measuring the uploads against the venue's author guidelines. Also
   skips compiling a LaTeX manuscript for its page count.

``--dont-check-hidden-text``
   Skip looking for white, invisible, microscopic, or off-page text. See
   :ref:`vc-hidden-text`.

``--dont-check-openreview-profiles``
   Skip looking up the authors' OpenReview profiles (OpenReview venues only).
   Avoids network requests. See :ref:`vc-openreview`.

``--anonymous``
   Check the submission for information that identifies its authors, as for a
   double-blind venue. Venues marked ``anonymous`` in ``venues.json`` (ICLR,
   AAAI) get this check automatically. Linked anonymous.4open.science
   repositories are fetched and scanned too, which needs network access. See
   :ref:`vc-anonymity`.

``--arxiv-latex-cleaner``
   Run `arxiv_latex_cleaner
   <https://github.com/google-research/arxiv-latex-cleaner>`_ on the LaTeX
   source first, then validate the cleaned copies. Applies to every ``.tex``
   upload (the cleaner runs on its whole directory) and every ``.zip``,
   ``.tar``, ``.tar.gz``, or ``.tgz`` bundle that contains a ``.tex`` file.
   The cleaner strips comments and drops unreferenced and auxiliary files.
   Cleaned copies are written next to the originals, as ``paper/`` →
   ``paper_arXiv/`` and ``source.zip`` → ``source_arXiv.zip``, replacing any
   earlier output. The originals and the ``.sub`` are not changed, so point the
   ``.sub`` at the cleaned copies to submit them. Needs the optional package:
   ``pip install "paperpush[validate]"``.

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
   combined with ``--dont-check-manuscript`` or ``--web``.

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

   paperpush validate biorxiv.sub.txt --dont-check-links --dont-check-references

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

Clean arXiv LaTeX source and check the result:

.. code-block:: bash

   paperpush validate arxiv.sub.txt --arxiv-latex-cleaner

See also
--------

- :doc:`../validation-checks` — every check in detail.
- :doc:`options` — look up the valid values for a field flagged as invalid.
- :doc:`requirements` — print the author-guideline rules the manuscript is
  measured against.
- :doc:`submit` — the next step once validation passes.
