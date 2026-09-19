``paperpush update-venues``
===========================

Fetch the latest published venue data now.

.. code-block:: text

   usage: paperpush update-venues [-h] [-v] [-q] [--clear]

Venue details (field options, help text, limits, and the author-guideline
rules in ``manuscript_requirements.json``) are published separately from
PaperPush releases, so a fix reaches you without upgrading. PaperPush checks for
new data on its own at most once a day and falls back to the data bundled with
the installed version when it is offline. Run this command to pick up a fix
right away, or to see which copy is in use.

Only changes that the installed version can use are applied. A new venue, a
new or changed field on an existing one, or a kind of manuscript rule the
installed version does not know yet needs a newer release
(``pip install -U paperpush``). Until then, that venue keeps its bundled
definition or rules. ``paperpush requirements VENUE`` shows which copy of the
rules it read.

Options
-------

``--clear``
   Delete the cached copy and use the data bundled with the installed version.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Environment
-----------

``PAPERPUSH_VENUE_DATA``
   ``auto`` (default) uses the published data. ``bundled`` uses only the
   data shipped with the installed version. A directory path reads
   ``venues.json`` and ``manuscript_requirements.json`` from that directory.

``PAPERPUSH_OFFLINE``
   Set to ``1`` to never fetch, while still using a previously cached copy.

Examples
--------

.. code-block:: console

   $ paperpush update-venues
   venue data updated (published 2026-09-19T10:02:11-07:00 (1514c9e))
   In use: published 2026-09-19T10:02:11-07:00 (1514c9e)
   Files: /home/me/.cache/paperpush/venue-data
