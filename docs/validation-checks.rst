What ``validate`` checks
========================

This page lists every check that :doc:`commands/validate` runs on a ``.sub``
file. ``paperpush submit`` runs the same checks before it opens a browser. The
MCP tool ``validate_subfile`` runs them too.

.. code-block:: bash

   paperpush validate biorxiv.sub

How findings are reported
-------------------------

Each finding is either an **error** or a **warning**:

- **Errors** block submission. They cover things paperpush can measure
  exactly: a blank required field, a missing file, an invalid option, a word or
  page count over the limit.
- **Warnings** are advisory. They cover heuristics over extracted text and
  images, such as a heading that was not found, a figure that looks
  low-resolution, or a name that might identify an author. Read each one and
  decide whether it applies.

Warnings print first, as ``warning: [field_id] message``. Errors follow, one
per line, under ``error: N problem(s) in FILE must be fixed before submitting``.
The field id in brackets tells you which line of the ``.sub`` to edit. It is
left out when the finding concerns the submission as a whole.

The exit code is ``0`` when there are no errors, ``1`` when there is at least
one error, and ``2`` when the ``.sub`` names an unknown venue. That makes it
easy to use ``validate`` as a gate in CI.

The passes at a glance
----------------------

.. list-table::
   :header-rows: 1
   :widths: 30 15 30 25

   * - Pass
     - Runs by default
     - Turn it off / on
     - Network
   * - :ref:`Fields <vc-fields>`
     - always
     - n/a
     - no
   * - :ref:`Multiple choice and option lists <vc-choices>`
     - always
     - n/a
     - no
   * - :ref:`Files, extensions, and sizes <vc-files>`
     - always
     - n/a
     - no
   * - :ref:`Word and page limits <vc-length>`
     - always
     - n/a
     - no
   * - :ref:`Author guidelines <vc-guidelines>`
     - yes
     - ``--dont-check-manuscript``
     - no
   * - :ref:`Link validation <vc-links>`
     - yes
     - ``--dont-check-links``
     - yes
   * - :ref:`Reference DOIs <vc-references>`
     - yes
     - ``--dont-check-references``
     - yes
   * - :ref:`Sensitive information and arXiv LaTeX <vc-sensitive>`
     - yes
     - ``--dont-check-for-sensitive-info``
     - no
   * - :ref:`Anonymization <vc-anonymity>`
     - double-blind venues only
     - ``--anonymous`` turns it on for any venue
     - yes (for linked repositories)

Every check reads the files the ``.sub`` points at: each ``file`` field, and
each line of a ``filelist`` field. Files inside ``.zip`` and ``.tar.gz`` source
bundles are read too. Nothing in your manuscript directory is modified.

.. _vc-fields:

Presence and shape of fields
----------------------------

These checks come from the field definitions in ``venues.json``. Every venue
gets them.

**Required fields** (error)
   A field marked ``required`` must have a value. The message names the field
   by its portal label, for example ``Title is required but empty``.

**Conditionally required fields** (error)
   A field with ``required_if`` becomes required once the field it depends on
   has a value. For example, ``funding_country`` is required once ``funding``
   is filled in. When the trigger is a yes/no question, only a *yes* makes the
   dependent field required.

**Confirmations and yes/no questions** (error)
   A ``boolean`` must read as yes or no. Accepted values are ``yes``/``no``,
   ``y``/``n``, ``true``/``false``, ``1``/``0``, and ``on``/``off``. A required
   confirmation, such as an author-consent checkbox, must be *yes*. A plain
   required yes/no question only needs an answer.

**Unknown fields** (error)
   A key that is not part of the venue's template is rejected. This is usually
   a typo in a field id.

**Whole numbers** (error)
   An ``int`` field must parse as an integer and fall inside the field's
   ``min_value`` and ``max_value`` bounds.

**Text length** (error)
   ``text`` and ``textarea`` fields are checked against their ``word_count``
   and ``character_count`` upper bounds and their ``min_character_count`` lower
   bound. Examples are an abstract word limit, a title character limit, and
   arXiv's 20-character minimum abstract. Words are whitespace-separated
   tokens.

**URLs** (error)
   On a field with ``require_url``, each non-blank line must be an ``http`` or
   ``https`` link with a host. A data-availability field that collects
   repository links is a typical case.

**Item counts** (error)
   ``min_count`` and ``max_count`` bound the number of items in a multi-item
   field. That means the options chosen in a ``multichoice``, or the lines of a
   ``filelist``, ``authorlist``, or list-style ``textarea``. An example is
   "suggest at least 4 reviewers".

**Author list** (error)
   The ``authorlist`` field is parsed into its ``|``-separated columns, and:

   - there is at least one author;
   - every author line has a name (or an OpenReview ID, on venues that use
     them);
   - every required column is filled in on every line (a column name ending in
     ``?`` is optional);
   - exactly one author is marked corresponding, when the venue has a
     ``corresponding`` column;
   - the corresponding author has an email, or every author does when the
     venue requires email for all.

**Structured text fields** (error)
   Other pipe-separated fields, such as funding sources and suggested
   reviewers, are checked line by line. Each required column must be present,
   and a missing column is reported by line number.

.. _vc-choices:

Multiple choice and option lists
--------------------------------

**Single choice** (error)
   A ``choice`` field must hold exactly one of the venue's ``options``. The
   error lists the valid values. The same rule applies to the nested
   drill-down fields that some portals use, like subject area and subcategory.

**Multiple choice** (error)
   A ``multichoice`` field is a comma-separated list. Every entry must be one
   of the options, and the number of entries must respect ``min_count`` and
   ``max_count``. When the option list is too long to print, which is the case
   for large taxonomies stored in an ``options_file``, the error tells you
   which ``paperpush options`` command lists the valid values.

**Recommended lists** (warning)
   Some portals offer a list of suggestions but also accept free text, like a
   keywords box or a dropdown with an "Other" entry. Such fields set
   ``options_recommended``. An off-list value there is a warning saying it will
   be entered as custom text, not an error.

**Per-file type labels** (error)
   A ``filelist`` whose lines carry a file-type column (``path | type``) must
   use one of the field's ``type_options``, such as ``Figure`` or
   ``Supplementary Material``.

**Venue-specific rules** (error)
   A venue's runner can add rules that the generic metadata cannot express.
   For example, Nature checks that each subject path exists in its category
   tree.

Use :doc:`commands/options` to look up the valid values for any field that is
flagged.

.. _vc-files:

Files, extensions, and sizes
----------------------------

These run for every ``file`` field and every line of a ``filelist``.

**File exists** (error)
   The path, with ``~`` expanded, must exist and must be a regular file, not a
   directory.

**File extension** (warning)
   When the field declares an ``accept`` list, a file whose extension is not
   on it is flagged. For example: ``cover.docx has extension '.docx'; Cover
   letter expects one of .pdf``. The author-guidelines pass below applies the
   same rule to each venue's documented manuscript, figure, table,
   supplementary, and cover-letter formats.

**Per-file size** (error)
   A file larger than the field's ``max_file_size_mb`` is an error. Sizes are
   in binary megabytes (MiB), which is how most file managers report them.

**Total upload size** (error)
   When the venue sets ``max_upload_mb``, the sizes of all uploads are added up
   and compared with the limit.

**PDF sanity** (error / warning)
   A ``.pdf`` must start with a ``%PDF-`` header, or it is an error. A PDF under
   1 KB, or one in which no pages can be found, is a warning because it is
   probably empty or broken.

.. _vc-length:

Word and page limits
--------------------

A manuscript ``file`` field can set a word limit and a page limit, each in two
scopes:

- **before references**: ``max_words_before_refs`` and
  ``max_pages_before_refs``. Venues that exclude an appendix or required
  statement from the main-text count list those headings in
  ``main_text_end_headings``, and counting stops at the first one.
- **whole document**: ``max_words`` and ``max_pages``.

Any of these limits can depend on another field through its ``*_by``
companion. For example, a page cap can vary with the article type chosen in the
``.sub``, so set the article type before you validate.

paperpush measures the uploaded file itself:

- **PDF**: text is extracted for the word count, and pages are counted
  directly.
- **LaTeX** (a ``.tex`` file, or a ``.zip`` source bundle): the source is
  compiled into a scratch directory with ``latexmk``, falling back to
  ``pdflatex``, and the resulting PDF's pages are counted. Your source
  directory is never written to.
- **Word** (``.docx``): words are counted from the document text. The page
  count comes from the page total Word saved in the file, if there is one.

A count over the limit is an **error**, for example
``Manuscript: 11 pages before references exceeds the 9-page limit``. When a
count cannot be taken, you get a **warning** that the limit was *not checked*.
This happens with a ``.doc`` binary, a format without fixed pages, or LaTeX
with no TeX toolchain installed. paperpush never assumes a limit was met.

.. _vc-guidelines:

Author guidelines (``manuscript_requirements.json``)
----------------------------------------------------

Many journals publish rules that their portal does not enforce. paperpush
records these in ``manuscript_requirements.json`` (see
:doc:`schemas/manuscript_requirements`) and measures the uploads against them.
The rules are resolved for the article type selected in the ``.sub``. Run
``paperpush requirements VENUE`` (:doc:`commands/requirements`) to print the
rules for a venue.

- **Manuscript**: file format and size, and the word and page limits above
  when ``venues.json`` does not already set them.
- **Required sections and declarations**: headings such as Introduction,
  Methods, and Results, and statements such as data availability, competing
  interests, and author contributions. Each is searched for under its usual
  wordings. A missing one is a warning.
- **Title page**: items that should leave a trace in the front matter, such as
  the title, a corresponding e-mail, an ORCID, a keywords line, a running
  title, and a word count.
- **Text limits from the .sub**: abstract, title, running-title, and keyword
  limits.
- **Figures**: format, file size, resolution (from the image metadata, or
  estimated from pixel width at the venue's print width), colour mode (for
  example RGB or CMYK), print dimensions, and the number of figures.
- **Tables and display items**: the number of tables, and the combined number
  of figures plus tables.
- **Supplementary files**: format, size, count, and whether the venue asks for
  a single combined PDF.
- **Cover letter**: format, and a warning when the guidelines ask for one and
  the field is empty.
- **References**: the number of entries in the reference list.
- **Total upload size**, when the portal does not set its own limit.

Measured numbers over a limit are errors. Checks that depend on extracting text
from a PDF or reading an image are warnings. A rule the portal already enforces
through ``venues.json``, such as an ``accept`` list, a per-file size cap, or a
field word limit, is reported only once, by that check.

.. _vc-links:

Link validation
---------------

paperpush collects every ``http(s)`` URL from the text of the uploads,
including LaTeX source, PDF text, and archive members. It removes duplicates
and probes each URL, several at a time. Only links that are **definitely
broken** are reported, as warnings:

- a ``404`` or ``410`` response: ``link … is not reachable (returned 404/gone)``;
- a GitHub repository that returns 404, which usually means it is still
  private, was renamed, or was deleted. The message asks you to make it public
  so reviewers can reach it.

Timeouts, bot walls, and other unclear responses are not reported, so a flaky
server does not produce false alarms. A server that rejects ``HEAD`` requests
is retried with ``GET``. Pass ``--dont-check-links`` to skip this pass when
working offline.

.. _vc-references:

Reference DOIs
--------------

Every DOI in the bibliography is resolved through ``doi.org`` and compared with
the reference that cites it. A warning is raised for a DOI that is:

- malformed;
- cited by two different references;
- not registered;
- registered to a different work. This usually means the DOI was copied from
  the wrong reference.

paperpush reads the bibliography in whatever form the submission uses. When
there is a ``.bib`` file, including one inside a source bundle, each entry's
title, first author, and year are compared field by field. Without one, the
reference list is read from the manuscript itself (PDF text, LaTeX source, or a
compiled ``.bbl``). Each DOI is then compared with the text of the entry it
sits in. A DOI outside the reference list, such as a dataset DOI in a
data-availability sentence, is only checked to see that it resolves. Pass
``--dont-check-references`` to skip this pass.

.. _vc-sensitive:

Sensitive information and arXiv LaTeX
-------------------------------------

Preprint servers publish your *source* files along with the PDF. This pass
looks for things that were never meant to become public. All findings are
warnings, and secrets are masked in the output so running the check does not
leak them into a log.

- **Credentials**: private-key headers, AWS, Google, GitHub, Slack, Stripe,
  OpenAI, and Anthropic keys, JSON Web Tokens, ``password = …`` and
  ``api_key: …`` style assignments, and URLs with an embedded password.
  ``.py`` files also go through ``bandit`` when it is installed. When it is
  not, a note suggests ``pip install bandit``.
- **GPS coordinates** in the EXIF data of figure photos.
- **Editable document links**: Google Docs, Sheets, Slides, or Forms links
  that anyone can open.
- **LaTeX comments**: ``%`` comments in the source, with special attention to
  notes such as ``TODO``, ``FIXME``, ``REVIEWER``, and ``CONFIDENTIAL``.
- **Unneeded files in a source bundle**: build and auxiliary files
  (``.aux``, ``.log``, ``.synctex.gz``, …), editor backups, ``.DS_Store``, and
  ``.git``, ``__MACOSX``, or ``.ipynb_checkpoints`` directories.
- **No code link**: a note when readable manuscript text links no GitHub
  repository at all, in case the paper has code that should be shared.

**arXiv LaTeX.** When the target is arXiv, or a venue that submits through
arXiv, and the scan finds LaTeX comments or unneeded files, one extra warning
suggests running `arxiv_latex_cleaner
<https://github.com/google-research/arxiv-latex-cleaner>`_ on the source
before you upload. arXiv publishes the source exactly as uploaded. A source
bundle that has already been cleaned does not trigger the warning.

Pass ``--dont-check-for-sensitive-info`` to skip this pass.

.. _vc-anonymity:

Anonymization (double-blind venues)
-----------------------------------

Venues marked ``"anonymous": true`` in ``venues.json`` (currently ICLR and
AAAI) get this pass on every ``validate``. For any other venue, add
``--anonymous``, or pass ``anonymous=true`` to the MCP tool ``validate_subfile``.
Every finding is a warning starting with ``anonymity:``. Take each one
seriously, because a leak can get a paper desk-rejected.

**What is searched.** The attached files, including archive members, and the
``.sub``'s own text fields (title, abstract, TL;DR, …), since reviewers see
those too. Every ``anonymous.4open.science`` repository linked from them is
also fetched and scanned (see below).

**What counts as identifying:**

- **The authors' own details**, taken from the ``.sub``'s author list: full
  names (two or more words, so a lone surname does not match ordinary prose),
  emails, ORCID iDs, OpenReview IDs, and affiliations (five or more characters).
  Matching ignores case and extra whitespace. A name that appears only in the
  reference list is *not* reported, because citing your own work in the third
  person is allowed. A conflicts-of-interest list is not treated as the
  authors.
- **Author metadata**: a PDF's ``/Author``, a Word, PowerPoint, or Excel file's
  creator or last-modified-by, and an image's EXIF Artist, Copyright, or
  XPAuthor.
- **Home-directory paths** such as ``/Users/<name>/``, ``/home/<name>/``, or
  ``C:\Users\<name>\``, which contain a username. Generic names like
  ``runner``, ``ubuntu``, and ``jovyan`` are ignored.
- **An Acknowledgments section**, as a plain heading, ``\section*{Acknowledgments}``,
  or an ``ack`` environment.
- **Camera-ready LaTeX switches** that print the author block:
  ``\iclrfinalcopy`` and ``\colmfinalcopy``, or a ``final``, ``camera-ready``,
  or ``preprint`` option on an ICLR, NeurIPS, ICML, AAAI, COLM, CoRL, or TMLR
  style package.

**Anonymous GitHub mirrors.** anonymous.4open.science only redacts the terms
you listed when you created the mirror. A name left in a LICENSE, a
``/Users/<name>/`` path in a notebook's output, or a ``github.com/<you>`` URL
is still visible. paperpush lists the mirror's files through the service's
public API and scans them like the uploads. It also flags a LICENSE copyright
line that names a holder, and it reports a mirror that has **expired**, **does
not exist**, is **still being anonymized**, or **has been removed**, since
reviewers would not be able to open it.

See also
--------

- :doc:`commands/validate`: usage, flags, and examples.
- :doc:`commands/options`: valid values for a flagged choice field.
- :doc:`commands/requirements`: the author-guideline rules for a venue.
- :doc:`schemas/venues`: the field attributes (``required``, ``accept``,
  ``max_pages``, ``anonymous``, …) that drive these checks.
