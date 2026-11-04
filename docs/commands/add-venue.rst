``paperpush add-venue``
=======================

Add a venue (journal, preprint server, or conference) to PaperPush, or correct
one, from a plain-text *venue file*.

.. code-block:: text

   usage: paperpush add-venue [-h] [-v] [-q] [--template] [--from SLUG] [-o OUT]
                              [--overwrite] [--dry-run] [--data-dir DIR] [FILE]

The workflow has two steps:

.. code-block:: bash

   paperpush add-venue --template my_journal     # writes my_journal.txt to fill in
   paperpush add-venue my_journal.txt            # checks it and adds the venue

The venue file has three parts, each going to one of the venue databases:

``[venue]``
   General information (slug, name, type, homepage, double-blind review), written
   to ``venues.json``.

``[form]`` and one ``[field ID]`` block per field
   The submission portal's form, in the order the portal asks for it, written to
   ``submission_forms.json``. The template starts with the fields most portals
   share (title, abstract, authors, manuscript, figures, cover letter, article
   type, declarations, ...). Delete those the portal lacks and add its own. Set
   ``include: no`` for a venue PaperPush only checks manuscripts against.

``[requirements]``, ``[requirements.SECTION]`` and ``[article_type NAME]``
   What the author guidelines require of the manuscript, written to
   ``manuscript_requirements.json`` and checked by :doc:`validate`. Every rule
   ``validate`` understands is listed, with its description; leave the ones the
   guidelines do not mention empty. An ``[article_type NAME]`` block lists only
   what differs for that article type, as ``section.key: value``.

The syntax is that of a ``.sub`` file: ``key: value``, or ``key: |`` followed by
lines indented two spaces. Lists are comma-separated or one item per line in a
``|`` block, booleans are ``yes``/``no``, and lines starting with ``#`` are
comments. Fill the file in by hand, or ask an AI agent to fill it from the
venue's portal and guideline pages.

Before anything is written, the file is checked the way PaperPush loads the
databases: unknown keys and bad values (with their line numbers), choice fields
without options, ``[article_type]`` names that are not options of the
article-type field, and a portal limit that differs from the guidelines' (a
warning). Submitting to a new venue also needs a runner script; see
``CONTRIBUTING.md``. Without one, ``subfile``, ``autofill`` and ``validate``
already work for the venue.

The website's **Add venue** button (next to the AI provider) fills in the same
file through a form and downloads it.

Options
-------

``FILE``
   The venue file to add. With ``--template``, the new venue's slug (optional).

``--template``
   Write a venue file to fill in, named ``SLUG.txt`` unless ``-o`` is given.

``--from SLUG``
   With ``--template``, start from an existing venue's entries, to correct them
   (add the file back with ``--overwrite``) or to add a sister journal (change the
   slug).

``-o OUT``, ``--output OUT``
   With ``--template``, where to write the file. ``-`` prints it.

``--overwrite``
   Overwrite an existing template file, or replace an existing venue's entries.
   (``--force`` is accepted as an older name.)
   A part the file leaves out (``include: no``) keeps the venue's existing entry.

``--dry-run``
   Check the file and print the entries it would add, without writing them.

``--data-dir DIR``
   Write the databases in ``DIR`` (copied there first when missing) instead of
   the installed package. Use them with ``PAPERPUSH_VENUE_DATA=DIR``.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Where the venue is written
--------------------------

In a source checkout, the venue goes into ``paperpush/``'s JSON files. Set
``PAPERPUSH_VENUE_DATA=bundled`` while you work on it so the published venue
data does not override your entries, then commit them and open a pull request
to share it. In a ``pip`` install, it goes into the
installed package and is replaced when PaperPush is upgraded, so send the venue
file in an issue to have it added for everyone. When ``PAPERPUSH_VENUE_DATA``
names a directory, that directory is read and written.
