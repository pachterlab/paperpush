``paperpush subfile``
=====================

Create a ``<venue>.sub`` submission template — the starting point for every
submission.

.. code-block:: text

   usage: paperpush subfile [-h] [-v] [-q] [-o OUTPUT] [--fill-defaults]
                            [--dont-fill-defaults] [--force]
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

Arguments
---------

``venue``
   The venue slug, e.g. ``biorxiv``, ``arxiv``, ``nature``. Run
   ``paperpush --venues`` to see every slug, or browse :doc:`../venues`.

Options
-------

``-o OUTPUT``, ``--output OUTPUT``
   Write the template to this path instead of ``<venue>.sub`` in the current
   directory.

``--fill-defaults``
   Pre-populate fields that have a default value with that default. **This is
   the default behaviour.**

``--dont-fill-defaults``
   Leave defaulted fields empty instead of pre-populating them, so you fill in
   every value yourself.

``--force``
   Overwrite an existing ``.sub`` file. Without this flag, ``subfile`` refuses
   to clobber a file that already exists.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Examples
--------

Create a template with defaults filled in:

.. code-block:: bash

   paperpush subfile arxiv

Write it somewhere specific and overwrite any existing file:

.. code-block:: bash

   paperpush subfile nature -o submissions/nature.sub --force

Create a bare template with no defaults pre-filled:

.. code-block:: bash

   paperpush subfile biorxiv --dont-fill-defaults

See also
--------

- :doc:`options` — list the allowed values for a single choice field.
- :doc:`autofill` — fill the template from a manuscript directory.
- Sample filled ``.sub`` files live in the repo under
  ``tests/sub_files/<venue>/<venue>.sub``.
