``paperpush validate``
======================

Run the pre-submission checks on a filled ``.sub`` file. Run this before
:doc:`submit` to catch problems while they are still cheap to fix.

.. code-block:: text

   usage: paperpush validate [-h] [-v] [-q] [--dont-check-links]
                             [--dont-check-for-sensitive-info]
                             [--dont-check-references]
                             [--dont-check-manuscript] [--anonymous]
                             [--dont-check-hidden-text]
                             [--dont-check-openreview-profiles]
                             [--arxiv-latex-cleaner] [-o [PATH]] [--alt-color]
                             [--ai] [--provider {anthropic,openai,google}]
                             [--model MODEL] [--ai-tasks FILE]
                             [--ai-results FILE]
                             subfile

Synopsis
--------

.. code-block:: bash

   paperpush validate biorxiv.sub
   paperpush validate biorxiv.sub -o              # also save biorxiv_validation.pdf
   paperpush validate biorxiv.sub --ai            # also run the AI checks via an API

``validate`` prints a report of every check, grouped and coloured by what
happened to it: **failed** (red), **warnings** (red), **not checked** --
because it could not run here, needs AI, or is not implemented yet (yellow) --
and **passed** (green). Errors must be fixed before the file can be submitted
(for example, a required field left blank, a referenced file that does not
exist, or a choice value that isn't one of the venue's allowed options);
warnings are worth reviewing but don't block submission. See
:ref:`how findings are reported <vc-report>` and :doc:`../validation-checks`.

The command exits non-zero if any errors are found, so it fits cleanly into
scripts and CI.

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
- Secrets and private data (:ref:`vc-sensitive`)
- Hidden text and prompt injection (:ref:`vc-hidden-text`)
- Anonymity, for double-blind venues (:ref:`vc-anonymity`)
- OpenReview profiles, for OpenReview venues (:ref:`vc-openreview`)

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
- Colour mode
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

Guideline rules no check reads are listed as **not implemented**.

The link, reference, sensitive-information, manuscript, hidden-text, and
OpenReview passes make network requests or read every uploaded file; each can
be turned off with its ``--dont-check-*`` flag below.

Arguments
---------

``subfile``
   Path to the ``.sub`` file to check, e.g. ``biorxiv.sub``.

``--dont-check-links``
   Skip probing the URLs cited in the uploaded files. Avoids network requests.

``--dont-check-references``
   Skip resolving the references' DOIs through ``doi.org``. Avoids network
   requests.

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

``-o [PATH]``, ``--output [PATH]``
   Also save the report. PDF by default, which keeps the colours; a path ending
   in ``.html``, ``.md``, or ``.txt`` saves that format instead. With no PATH,
   writes ``<subfile>_validation.pdf`` next to the ``.sub``.

``--alt-color``
   Use colour-blind-friendly colours (blue for passed, vermillion for failed,
   grey for not checked) in the terminal and the saved report.

``--ai``
   Also run the checks that need a reader's judgement through an LLM API, with
   the manuscript, figures, and other files attached. Uses whichever of
   ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, or ``GEMINI_API_KEY`` is set and
   the provider's SDK (``pip install "paperpush[autofill]"``). If the request
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

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags. Use ``-v``
to see the checks as they run.

Examples
--------

.. code-block:: bash

   paperpush validate biorxiv.sub

Fail a CI job if the submission isn't ready:

.. code-block:: bash

   paperpush validate biorxiv.sub || exit 1

Run the field and file checks alone, with no network access:

.. code-block:: bash

   paperpush validate biorxiv.sub --dont-check-links --dont-check-references

Save a colour-blind-friendly HTML report:

.. code-block:: bash

   paperpush validate biorxiv.sub -o report.html --alt-color

Let an agent do the AI checks, then merge its answers:

.. code-block:: bash

   paperpush validate biorxiv.sub --ai-tasks tasks.json
   # ... the agent writes results.json ...
   paperpush validate biorxiv.sub --ai-results results.json -o

Clean arXiv LaTeX source and check the result:

.. code-block:: bash

   paperpush validate arxiv.sub --arxiv-latex-cleaner

See also
--------

- :doc:`../validation-checks` — every check in detail.
- :doc:`options` — look up the valid values for a field flagged as invalid.
- :doc:`requirements` — print the author-guideline rules the manuscript is
  measured against.
- :doc:`submit` — the next step once validation passes.
