``paperpush validate``
======================

Run the pre-submission checks on a filled ``.sub`` file. Run this before
:doc:`submit` to catch problems while they are still cheap to fix.

.. code-block:: text

   usage: paperpush validate [-h] [-v] [-q] [--dont-check-links]
                             [--dont-check-for-sensitive-info]
                             [--dont-check-references]
                             [--dont-check-manuscript]
                             subfile

Synopsis
--------

.. code-block:: bash

   paperpush validate biorxiv.sub

``validate`` reports two severities:

- **Errors** must be fixed before the file can be submitted (for example, a
  required field left blank, a referenced file that does not exist, or a choice
  value that isn't one of the venue's allowed options).
- **Warnings** are worth reviewing but don't block submission.

The command exits non-zero if any errors are found, so it fits cleanly into
scripts and CI.

What it checks
--------------

- **Required fields** are present and non-empty.
- **Files exist** — every ``file`` / ``filelist`` path (manuscript, figures,
  cover letter, supplements) resolves to a real file on disk.
- **Choice values are valid** — every ``choice`` field holds one of the venue's
  allowed options, including nested drill-down fields.
- **Structured fields are well-formed** — for example, author lists parse and
  mark exactly one corresponding author.
- **Cited links resolve** — the URLs in the uploaded files are probed, and ones
  that are definitively broken (404/gone, including a still-private GitHub
  repository) are reported as warnings.
- **Reference DOIs point at the right paper** — every DOI in the submission's
  bibliography is resolved through ``doi.org`` and held against the reference
  citing it. A DOI that is malformed, cited by two references, unregistered, or
  registered to a different work — the usual sign of a DOI copied from the wrong
  reference — is reported as a warning.

  The bibliography is read whichever way the submission ships it. If there is a
  ``.bib`` file (including one bundled in a source archive), its entries are
  compared field by field: title, first author, and year. Otherwise the
  manuscript's own reference list is read — from a PDF's extracted text, LaTeX
  source, or a compiled ``.bbl`` — and each DOI is compared against the text of
  the reference entry it sits in, so no ``.bib`` is needed. Only DOIs inside the
  reference list are compared this way; a dataset DOI in a data-availability
  sentence is merely checked that it resolves.
- **Nothing private is about to be published** — the uploaded files are scanned
  for API keys, passwords, private keys, GPS coordinates in figures,
  editable-document links, and LaTeX source comments.
- **The manuscript meets the venue's author guidelines** — the uploads are
  measured against the rules recorded for the venue in
  ``manuscript_requirements.json`` (see :doc:`requirements` to print them):

  - the manuscript file's format and size, and its word and page count against
    the venue's limits, for the article type selected in the ``.sub``. A
    ``.tex`` manuscript (or a ``.zip`` LaTeX bundle) is compiled to a scratch
    PDF with ``latexmk`` or ``pdflatex`` for its page count; the source
    directory is never written to, and without a TeX toolchain the page limit
    is reported as unchecked;
  - the section headings and declarations the venue requires (Introduction,
    Methods, Results, ...; data availability, competing interests, author
    contributions, ...), found in the manuscript text under any of their usual
    wordings;
  - title-page items that leave a trace in the front matter — the title, a
    corresponding e-mail, an ORCID, a keywords line, a running title, a word
    count;
  - abstract, title, running-title, and keyword limits read from the ``.sub``;
  - each figure's format, file size, resolution (from its metadata, or judged
    from its pixel width at the venue's print width), colour mode, and print
    dimensions, and the number of figures, tables, and display items;
  - supplementary-file format, size, and single-PDF rules; the cover letter;
    the reference count; the total upload size.

  Measured numbers that exceed a limit are errors; anything that depends on
  extracting text from a PDF or reading an image is a warning. A rule the
  portal already enforces through ``venues.json`` (an ``accept`` list, a
  per-file size cap, a word limit on the field) is reported once, by that
  check, not again here.

The last four passes make network requests or read every uploaded file; each
can be turned off with its ``--dont-check-*`` flag below.

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

See also
--------

- :doc:`options` — look up the valid values for a field flagged as invalid.
- :doc:`requirements` — print the author-guideline rules the manuscript is
  measured against.
- :doc:`submit` — the next step once validation passes.
