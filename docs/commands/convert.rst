``paperpush convert``
=====================

Convert a manuscript between Markdown, Word, and LaTeX.

.. code-block:: text

   usage: paperpush convert [-h] [-v] [-q] --to {docx,tex} [--manuscript FILE]
                            [-o DIR] [--bib FILE] [--assets DIR]
                            [--tracked-changes {accept,reject}]
                            [--citation-style {numeric,author-year}]
                            [--no-source] [--no-compile] [--strict]
                            [--update-sub] [--json]
                            [subfile]

.. code-block:: bash

   paperpush convert --manuscript paper.tex --to docx
   paperpush convert --manuscript paper.docx --to tex
   paperpush convert --manuscript paper.md --to docx --bib refs.bib
   paperpush convert nature.sub.txt --to docx --no-source --update-sub

The directions are Markdown to Word, Markdown to LaTeX, and Word to LaTeX and
back. The original file is never modified. The result goes to a new directory
(``converted_<format>`` next to the manuscript, or ``-o DIR``) together with
``CONVERSION.md``.

What carries over
-----------------

- **Equations** become native, editable Word equations, and Word equations
  become LaTeX.
- **Citations** stay live. In Word they are fields that keep their citation
  keys. Zotero, Mendeley, and EndNote citations in a Word file become BibTeX
  entries.
- **Cross-references** to sections, figures, tables, and equations are Word
  fields and ``\ref`` commands.
- **Figures** Word cannot embed (PDF, EPS, TikZ) are rendered to 300 dpi PNG.
- **Macros** defined with ``\newcommand`` are expanded, including simple ones
  from a venue's ``.sty`` beside the manuscript.

Going to Word and back
----------------------

A ``.docx`` made from LaTeX carries the original source. Converting it back
restores every block nobody edited exactly as written, along with the preamble
and title block. Only edited paragraphs are generated, so a coauthor's changes
in Word become a small diff in your LaTeX.

The source is hidden in the file. Pass ``--no-source`` for a ``.docx`` that goes
to a journal: LaTeX can hold text a reader cannot see, such as ``\todo`` notes
or, on a double-blind submission, author names. A file made that way still
converts to LaTeX, but from what is visible in it.

Away from the project, pass ``--assets DIR`` with the directory holding the
original figures and the venue's ``.cls``, ``.sty``, and ``.bst`` files.

Word files written by hand
--------------------------

Structure is taken from what Word records and, where it records nothing, from
conventions. These are listed in ``CONVERSION.md`` so they can be checked:

- "[3]", superscripts, and "(Smith, 2020)" become citations when they match the
  reference list.
- "Figure 2" becomes a cross-reference when a figure has that number.
- A short bold line becomes a heading when the file uses no heading styles.
- Tracked changes are accepted and counted (``--tracked-changes`` chooses).
- Comments become LaTeX comments.

MathType equations cannot be read: convert them to Word equations first.

The report
----------

Every conversion re-reads its own output and compares it with the source:
headings, words, equations, figures, tables, citations, cross-references, and
footnotes. ``CONVERSION.md`` shows the table and lists anything to check. LaTeX
output is also compiled, unless ``--no-compile`` is given. With ``--strict`` the
command exits 1 when the result needs review.

The MCP server exposes the same as ``convert_manuscript``.
