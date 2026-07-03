``paperpush validate``
======================

Run the pre-submission checks on a filled ``.sub`` file. Run this before
:doc:`submit` to catch problems while they are still cheap to fix.

.. code-block:: text

   usage: paperpush validate [-h] [-v] [-q] subfile

Synopsis
--------

.. code-block:: bash

   paperpush validate biorxiv.sub

``validate`` reports two severities:

- **Errors** must be fixed before the file can be submitted (for example, a
  required field left blank, a referenced file that does not exist, or a choice
  value that isn't one of the venue's allowed options).
- **Warnings** are worth reviewing but don't block submission.

The command exits non-zero if any errors are found, so it fits cleanly into
scripts and CI.

What it checks
--------------

- **Required fields** are present and non-empty.
- **Files exist** — every ``file`` / ``filelist`` path (manuscript, figures,
  cover letter, supplements) resolves to a real file on disk.
- **Choice values are valid** — every ``choice`` field holds one of the venue's
  allowed options, including nested drill-down fields.
- **Structured fields are well-formed** — for example, author lists parse and
  mark exactly one corresponding author.

Arguments
---------

``subfile``
   Path to the ``.sub`` file to check, e.g. ``biorxiv.sub``.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags. Use ``-v``
to see the checks as they run.

Examples
--------

.. code-block:: bash

   paperpush validate biorxiv.sub

Fail a CI job if the submission isn't ready:

.. code-block:: bash

   paperpush validate biorxiv.sub || exit 1

See also
--------

- :doc:`options` — look up the valid values for a field flagged as invalid.
- :doc:`submit` — the next step once validation passes.
