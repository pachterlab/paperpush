``paperpush find-reviewers``
============================

Suggest peer reviewers for a manuscript: researchers who recently published
similar work, with the manuscript's authors, their co-authors, and their
colleagues left out.

.. code-block:: text

   usage: paperpush find-reviewers [-h] [-v] [-q] [--manuscript PATH]
                                   [--title TEXT] [--abstract TEXT]
                                   [-k KW[,KW...]] [--author AUTHOR]
                                   [--exclude NAME] [-n N] [--years N]
                                   [--write] [--json]
                                   [SUBFILE|MANUSCRIPT]

Many venues ask for suggested reviewers. ``find-reviewers`` reads
`OpenAlex <https://openalex.org>`_, which indexes most published work and its
authors:

1. The works most similar to the manuscript from the last six years
   (``--years``) are found the same way as for ``paperpush pick-venue`` (see
   :doc:`pick-venue`).
2. Each of their authors scores the work's similarity, with recent work
   counting more. First and last authors count fully and middle authors less,
   and a paper with a long author list counts less for each author. An
   h-index under 20 lowers a candidate's score a little (down to half at 0),
   to favour established researchers.
3. Left out, before the list is cut to ``-n``:

   - the manuscript's authors, looked up in OpenAlex by ORCID iD when one is
     given and otherwise by name (preferring a record at their stated
     institution);
   - anyone who co-authored a work with one of them in the last four years;
   - anyone at one of their institutions (on the similar work, or their last
     known institution);
   - every ``--exclude`` name, and the reviewers the ``.sub`` already opposes
     or excludes.

Each suggestion shows the reviewer's last known institution, ORCID iD,
OpenAlex id, h-index, and the similar works that put them on the list. The
output also lists who was left out and why.

**E-mail addresses.** OpenAlex has none, so the e-mail column is left blank:
look each address up (an institution page, a recent paper's corresponding
address) before submitting. Venues that require reviewer e-mails fail
``paperpush validate`` until you do.

**Check the list yourself.** OpenAlex author records are matched by name and
affiliation, which are not always clean. A conflict OpenAlex does not record
(a close colleague, a competitor, a co-author from long ago) is not screened
out. Without an author list -- no ``.sub`` and no ``--author`` -- nothing is
screened out at all, and a note says so.

This needs network access. Set ``OPENALEX_API_KEY`` to use your own OpenAlex
key instead of the free tier shared by everyone on the same IP address. No
e-mail address or other personal information is sent.

Synopsis
--------

.. code-block:: bash

   paperpush find-reviewers cell.sub.txt                  # title, abstract, keywords, and authors from the .sub
   paperpush find-reviewers cell.sub.txt --write          # ... and add them to its reviewer field
   paperpush find-reviewers manuscript.pdf --author "Ada Lovelace | University of London 0000-0001-2345-6789"
   paperpush find-reviewers --title "$TITLE" --abstract "$(cat abstract.txt)" --exclude "Charles Babbage"
   paperpush find-reviewers cell.sub.txt --json -n 20     # for scripts and agents

Options
-------

``SUBFILE|MANUSCRIPT``
   A ``.sub`` file or a manuscript. From a ``.sub``, the title, abstract,
   keywords, authors, and the reviewers it opposes or excludes are read; when
   it has no title or abstract, its manuscript file is read for them. Any
   other file is read as the manuscript (``.tex``, ``.zip``, ``.docx``,
   ``.pdf``, or a ``.txt``/``.md`` holding the title and abstract), the same
   way ``pick-venue`` reads it.

``--manuscript PATH``
   A manuscript to read the title, abstract, and keywords from.

``--title TEXT``, ``--abstract TEXT``
   The manuscript's title and abstract. They replace what was read from a
   file.

``-k KW[,KW...]``, ``--keywords KW[,KW...]``
   Keywords, comma-separated; may be repeated.

``--author AUTHOR``
   A manuscript author, as ``"Name"`` or ``"Name | affiliation"``, so that they, their co-authors, and their colleagues
   are left out. An ORCID iD anywhere in the value is used to find them. May be
   repeated; added to the authors in the ``.sub``.

``--exclude NAME``
   Someone not to suggest, such as an opposed reviewer. May be repeated.

``-n N``, ``--limit N``
   Suggest at most N reviewers. Default: 10.

``--years N``
   Consider similar work from the last N years. Default: 6.

``--write``
   Add the suggestions to the ``.sub``'s suggested-reviewer field
   (``reviewers`` or ``suggested_reviewers``), in that field's column order,
   with the e-mail column blank. Lines already there are kept, and a reviewer
   already listed is not added again. Fails for a venue whose form has no
   reviewer field.

``--json``
   Print the result as JSON: ``reviewers`` (each with ``name``, ``email``
   (always empty), ``institution``, ``orcid``, ``openalex_id``, ``score``,
   ``works_count``, ``h_index``, and ``similar_works``), ``excluded`` (each
   with ``name``, ``reason``, and ``openalex_id``), the ``search`` used,
   ``similar_works``, ``since_year``, the manuscript authors found and not
   found in OpenAlex, and ``notes``. The MCP tool ``find_reviewers`` returns
   the same, with ``subfile``, ``manuscript``, ``authors``, ``exclude``, and
   ``write`` as arguments.

Exit status
-----------

0 when at least one reviewer is suggested. 1 when none is left after the
conflicts are removed, or when OpenAlex could not be reached. 2 for bad
arguments.
