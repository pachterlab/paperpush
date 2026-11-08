``paperpush format``
====================

Reformat a Word or LaTeX manuscript for a venue: with the venue's own template
when paperpush can find one, otherwise from the layout rules it records for
the venue.

.. code-block:: text

   usage: paperpush format [-h] [-v] [-q] [--manuscript FILE] [--venue VENUE]
                           [-o DIR] [--template URL_OR_PATH] [--no-template]
                           [--list-templates] [--no-browser]
                           [--keep-direct-formatting] [--no-compile]
                           [--update-sub] [--json]
                           [subfile]

.. code-block:: bash

   paperpush format nature.sub.txt               # the .sub's venue and manuscript
   paperpush format --venue iclr_2027 --manuscript paper.tex
   paperpush format --venue frontiers_genetics --list-templates
   paperpush format --venue cell --manuscript ms.docx --template ~/Downloads/cell_template.dotx

The original files are never modified. The result goes to a new directory
(``formatted_<venue>`` next to the manuscript, or ``-o DIR``) together with
``CHANGES.md``, which lists every change, what is left for you, and how the
result measures against the venue's layout rules.

Finding the template
--------------------

The venue's template comes first. paperpush looks, in order, at:

1. the template URL recorded for the venue (``manuscript.template_url`` in
   :doc:`../schemas/manuscript_requirements`): a file, an archive, a GitHub
   repository, or a page that links one;
2. every author-guideline page recorded for the venue, for links that look like
   a manuscript template: a ``.docx``/``.dotx``, a ``.zip``, a ``.cls`` or
   ``.sty``, an Overleaf template, a GitHub style-file repository, or a
   "download the template package" link. Pages those links lead to are read one
   level further. Forms, checklists, copyright and cover-letter templates,
   supplementary-material templates, and book or monograph templates are
   ignored;
3. for LaTeX, the document class recorded for the venue
   (``manuscript.latex_class``): from your TeX installation when it has it,
   otherwise from CTAN (a class shipped only as ``.dtx``/``.ins`` sources is
   generated with ``latex``).

Pages are read over HTTPS and, when a publisher's bot check turns that away,
once more in a headless browser (``--no-browser`` skips that). Pages that still
cannot be read are listed. A Word manuscript uses a Word template and a LaTeX
manuscript a LaTeX one; a template of the other kind is listed in
``CHANGES.md`` but not applied. Overleaf templates cannot be downloaded
without signing in: download the project as a zip and pass it with
``--template``. Downloads are cached in ``~/.cache/paperpush/templates``.

``--list-templates`` only runs the search and prints what it found.

Applying a Word template
------------------------

- **Styles**: the template's styles replace the manuscript's. A manuscript
  style with the same name as a template style (``heading 1``, ``Normal``,
  ``Caption``, ...) becomes that style, whatever its id; the manuscript's own
  styles the template lacks are kept.
- **Theme**: the template's theme fonts and colors.
- **Page setup**: page size, margins, columns, text grid, and line and page
  numbering, in every section.
- **Headers and footers**, with any images they use.
- **Direct formatting**: fonts, sizes, and line spacing set directly on the
  text are removed so the template's styles show. Text set in a symbol font
  keeps it, a size well above the body size (a heading formatted by hand)
  is kept, and tables keep their own spacing. ``--keep-direct-formatting``
  leaves it all.

The venue's own layout rules are then applied on top, so a guideline the
template does not carry (line numbers for review, say) is still met.

Applying a LaTeX template
-------------------------

The manuscript's sources are copied, and only the copy of the main ``.tex`` is
edited:

- the template's ``.cls``, ``.sty``, and ``.bst`` files are copied in;
- ``\documentclass`` becomes the template's, with the options its sample paper
  uses (``\documentclass[preprint,12pt]{elsarticle}``);
- the template's own style packages are loaded as the sample loads them
  (``\usepackage{iclr2027_conference,times}``);
- packages that set the page layout the class owns (``geometry``,
  ``fullpage``, ``a4wide``, ...) are commented out, never deleted;
- ``\bibliographystyle`` becomes the template's; when the template ships
  several samples (numbered and author-year), the one matching the venue's
  reference style is used;
- ``\author{A \and B}`` is split into one ``\author`` per author for a class
  that expects that (one that does not define ``\and``).

The result is compiled when a TeX installation is found, and ``CHANGES.md``
says why if it does not compile (a package your installation lacks is named,
with the ``tlmgr`` command to install it). A class that wants its front matter
in its own form (``\begin{frontmatter}``, ``\affiliation``, ...) is reported,
with the template's sample to copy from. A ``.zip`` source bundle is
re-bundled.

Without a template
------------------

The layout rules recorded for the venue are applied.

- **Word**: page size, margins, body font and size (document defaults and the
  Normal style), double spacing, continuous line numbers, and a centered page
  number in the footer when there is none.
- **LaTeX**: ``geometry`` for page size and margins, ``setspace`` for double
  spacing, ``lineno`` for line numbers, ``mathptmx`` (Times) or ``helvet``
  (Arial/Helvetica) for the font, and the size as a class option for the
  standard classes. The manuscript's own ``geometry``/``setspace``/``lineno``
  setup is commented out.

What ``format`` does not do
---------------------------

These are listed in ``CHANGES.md`` when the venue has rules for them:
converting the reference style (``validate`` and the ``reference-checking``
skill), moving figures, reordering sections, splitting out a title page, and
anonymizing. PDFs and legacy ``.doc`` files cannot be reformatted; Markdown is
converted with `pandoc <https://pandoc.org>`_ when it is installed.

Afterwards, point the ``.sub`` at the new file (``--update-sub`` does it) and
run :doc:`validate`.

The MCP server exposes the same as ``format_manuscript``.
