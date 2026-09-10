``paperpush requirements``
==========================

Show what a venue's author guidelines require of the manuscript itself: file
formats and size caps, word and page limits, the sections and statements the
text must contain, title-page items, figure resolution and dimensions,
reference style and count, supplementary-file rules. These are the rules
:doc:`validate` measures the uploaded files against.

.. code-block:: text

   usage: paperpush requirements [-h] [-v] [-q] [--article-type TYPE] [--json]
                                 venue

Synopsis
--------

.. code-block:: bash

   paperpush requirements nature

prints a readable summary grouped by section, for example:

.. code-block:: text

   Manuscript requirements for Nature (nature)
     read from the author guidelines on 2026-09-10
     https://www.nature.com/nature/for-authors/formatting-guide
     article types with their own rules: Matters Arising, Submit - Review (use --article-type)

   manuscript:
     formats: .pdf, .docx, .doc
     max words before refs: 3500
     max display items: 6
     line numbers: True
     ...

   figures:
     min dpi: 300
     single column width mm: 89
     double column width mm: 183
     ...

Where the rules live
--------------------

The rules come from ``paperpush/manuscript_requirements.json``, the companion
to ``venues.json``. ``venues.json`` describes the *submission form* -- the
fields a portal asks for and what each accepts. ``manuscript_requirements.json``
describes what the venue's *author guidelines* say about the manuscript: every
entry uses the same section keys (``manuscript``, ``title_page``, ``abstract``,
``keywords``, ``sections``, ``statements``, ``figures``, ``tables``,
``supplementary``, ``references``, ``cover_letter``, ``upload``) so the same
vocabulary describes every venue, and records only what the guidelines state.
Each entry names the pages it was read from (``source_urls``) and when
(``retrieved``).

Two mechanisms keep entries short: ``inherits`` lets one venue start from
another's rules and override a few keys, and ``article_types`` holds
per-article-type overrides keyed by the venue's own article-type option, which
``validate`` applies according to the article type selected in the ``.sub``.

Arguments
---------

``venue``
   Venue slug, e.g. ``nature`` (see ``paperpush --venues``).

``--article-type TYPE``
   Show the rules for this article type -- the exact option string of the
   venue's article-type field -- with its overrides applied, instead of the
   default research-article rules.

``--json``
   Print the requirements as JSON (the resolved entry) instead of the readable
   summary.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Exit status
-----------

``0`` when the requirements were printed, ``1`` when the venue has no
requirements recorded, ``2`` when the venue is unknown.

See also
--------

- :doc:`validate` — measures the manuscript and other uploads against these
  rules (``--dont-check-manuscript`` turns that off).
- :doc:`options` — the allowed values of a ``choice`` field, including the
  article-type field whose values key the per-type overrides.
