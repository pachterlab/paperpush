``paperpush subfile``
=====================

Create a ``<venue>.sub`` submission template — the starting point for every
submission.

.. code-block:: text

   usage: paperpush subfile [-h] [-v] [-q] [-o OUT] [--dont-fill-defaults]
                            [--overwrite] [--web] [--venue-order]
                            venue

Synopsis
--------

.. code-block:: bash

   paperpush subfile biorxiv

This writes ``biorxiv.sub`` in the current directory: a commented, YAML-like
file listing every field the venue requires. Each field carries its type
(``text``, ``textarea``, ``choice``, ``file``, ``filelist``, ``authorlist``,
…), whether it is required, a short description, and — for choice fields — the
allowed options. Fill in the values, then move on to :doc:`autofill` or
:doc:`validate`.

Lines beginning with ``#`` are comments and are ignored when the file is read.

Field order
-----------

Every venue's template lists its fields under the same section headings, in the
same order, so ``.sub`` files for different venues line up and can be compared
side by side:

1. Manuscript type (article type, target journal, revision status)
2. Files (manuscript, figures, tables, supplements, cover letter)
3. Manuscript details (title, short title, abstract, keywords, subject areas)
4. Authors
5. Reviewers and editors
6. Declarations (funding, competing interests, data and code availability,
   consent checkboxes)
7. Additional information

Within a section, fields that many venues share come first, in a fixed order
(e.g. title, then short title, then abstract), followed by the venue's own
fields in the order its portal asks for them. A follow-up question (such as a
previous DOI when the manuscript is a revision) always comes after the question
it depends on. Pass ``--venue-order`` to list the fields in the order the
venue's portal asks for them instead, with no section headings. The order never
changes what ``validate`` or ``submit`` do: fields are read by id.

Arguments
---------

``venue``
   The venue slug, e.g. ``biorxiv``, ``arxiv``, ``nature``. Run
   ``paperpush venues`` to see every slug, or browse :doc:`../venues`.

Options
-------

``-o OUT``, ``--output OUT``
   Write the template to this path. Default: ``<venue>.sub`` in the current
   directory (e.g. ``biorxiv.sub``). ``.sub`` is added if OUT lacks it.

``--dont-fill-defaults``
   Fields that have a default value are pre-populated with it unless you pass
   this flag; with it, they are left empty so you fill in every value yourself.

``--overwrite``
   Overwrite an existing ``.sub`` file. Without this flag, ``subfile`` refuses
   to clobber a file that already exists. (``--force`` is accepted as an older
   name.)

``--web``
   After writing the template, open it in the paperpush web page on this
   computer (served on 127.0.0.1 until Ctrl-C) to fill it in through a form.
   Every edit is saved to the ``.sub`` file. If the file already exists, it is
   opened instead of refused. Needs ``pip install 'paperpush[web]'``.

   Without ``--web``, the command still ends by printing a link to
   ``http://127.0.0.1:PORT/...`` that shows the ``.sub`` in the web page, served by
   a background process that stops after 30 minutes unused. It is printed only to
   a terminal with the web extra installed; set ``PAPERPUSH_NO_WEB_LINK=1`` to
   turn it off.

``--venue-order``
   List the fields in the order the venue's submission form asks for them,
   instead of grouped into the sections every venue shares (see `Field order`_).
   Handy when filling the template while looking at the portal.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Examples
--------

Create a template with defaults filled in:

.. code-block:: bash

   paperpush subfile arxiv

Write it somewhere specific and overwrite any existing file:

.. code-block:: bash

   paperpush subfile nature -o submissions/nature.sub --overwrite

Create a bare template with no defaults pre-filled:

.. code-block:: bash

   paperpush subfile biorxiv --dont-fill-defaults

List the fields in the portal's own order:

.. code-block:: bash

   paperpush subfile nature --venue-order

See also
--------

- :doc:`options` — list the allowed values for a single choice field.
- :doc:`autofill` — fill the template from a manuscript directory.
- Sample filled ``.sub`` files live in the repo under
  ``tests/sub_files/<venue>/<venue>.sub``.
