``paperpush venues``
====================

List the venues PaperPush supports.

.. code-block:: text

   usage: paperpush venues [-h] [-v] [-q] [--validate] [--submit] [-k KW[,KW...]]
                           [-t TYPE] [--columns COL[,COL...]]
                           [--hide-columns COL[,COL...]]

Prints one table of every supported venue, grouped by type (preprint server,
journal, conference), with its slug, subject keywords, link, and whether
PaperPush can validate a manuscript against it and submit to it. See
:doc:`../venues` for the full list.

Filters combine: a venue is listed only when it passes all of them.

.. code-block:: bash

   paperpush venues                                   # every venue
   paperpush venues --submit                          # venues paperpush can submit to
   paperpush venues --submit -t journal -k biology    # biology journals paperpush can submit to
   paperpush venues --columns slug,submit             # just two columns

Options
-------

``--validate``
   Only venues PaperPush can validate a manuscript against (``paperpush validate``).

``--submit``
   Only venues PaperPush can submit to (``paperpush submit``). Validate-only
   venues are left out.

``-k KW[,KW...]``, ``--keywords KW[,KW...]``
   Only venues tagged with any of these comma-separated subject keywords, from
   the fixed list: general, biology, genetics, bioinformatics, immunology,
   neuroscience, medicine, radiology, machine learning, computer vision,
   natural language processing, robotics, math, statistics, physics, and
   computer science. An unknown keyword is an error that suggests the closest
   match.

``-t TYPE``, ``--type TYPE``
   Only venues of this kind: ``preprint``, ``journal``, or ``conference``.
   Repeat it to allow several kinds.

``--columns COL[,COL...]``
   Show only these comma-separated columns, in this order. Columns: ``slug``,
   ``venue``, ``keywords``, ``type``, ``citedness``, ``link``, ``validate``,
   ``submit``.

   ``citedness`` (shown as *2-yr citedness*) is OpenAlex's 2-year mean
   citations per work: an open counterpart of the Journal Impact Factor, not
   the Clarivate figure, and not comparable across fields. It is blank for
   preprint servers and for venues paperpush has not matched to OpenAlex.

``--hide-columns COL[,COL...]``
   Hide these comma-separated columns. Combines with ``--columns``.
