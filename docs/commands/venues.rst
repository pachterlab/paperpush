``paperpush venues``
====================

List the venues PaperPush supports.

.. code-block:: text

   usage: paperpush venues [-h] [-v] [-q] [--validate] [--submit] [-k KW[,KW...]]
                           [-t TYPE] [--columns COL[,COL...]]
                           [--hide-columns COL[,COL...]]

Prints one table of every supported venue, grouped by type (preprint server,
journal, conference), with its slug, subject keywords, link, and whether
PaperPush can validate a manuscript against it and submit to it. Each venue's
rank and a conference's next deadlines are available as extra columns (below)
and in `venues.md <https://github.com/pachterlab/paperpush/blob/main/venues.md>`_. See
:doc:`../venues` for the full list.

Filters combine: a venue is listed only when it passes all of them.

.. code-block:: bash

   paperpush venues                                   # every venue
   paperpush venues --submit                          # venues paperpush can submit to
   paperpush venues --submit -t journal -k biology    # biology journals paperpush can submit to
   paperpush venues --columns slug,submit             # just two columns
   paperpush venues -t conference --columns slug,rank,deadline  # ranks and deadlines

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
   ``venue``, ``keywords``, ``type``, ``rank``, ``deadline``, ``link``,
   ``validate``, ``submit``. ``rank`` and ``deadline`` are left out unless
   named here (``--columns slug,rank,deadline``); the full table, with both,
   is in `venues.md <https://github.com/pachterlab/paperpush/blob/main/venues.md>`_.

   ``deadline`` (shown as *Next deadline*) gives a conference's next abstract
   and paper deadlines, one per line, from `conference-agent
   <https://conferenceagent.vercel.app>`_ and `ccfddl <https://ccfddl.com>`_
   (cached for a day; ``~`` marks last cycle's dates moved forward a year).
   It appears only when the list includes a conference.

   ``rank`` lists everything paperpush knows about a venue's standing, one
   item per line, and is blank when it knows nothing:

   - ``citedness``: OpenAlex's 2-year mean citations per work, an open
     counterpart of the Journal Impact Factor (not the Clarivate figure), not
     comparable across fields. Any venue paperpush has matched to OpenAlex,
     which in practice means journals.
   - ``CORE``: the conference's `CORE <https://portal.core.edu.au/conf-ranks/>`_
     rank (A*, A, B, C).
   - ``CCF``: its China Computer Federation rank (A, B, C), from ccfddl.
   - ``accept rate``: its latest acceptance rate and the year, from ccfddl.
   - ``size``: small, medium, large, or massive, from conference-agent.

   The last four apply to conferences only. The column is dropped when no
   listed venue has any of them.

``--hide-columns COL[,COL...]``
   Hide these comma-separated columns. Combines with ``--columns``.
