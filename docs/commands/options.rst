``paperpush options``
=====================

List the allowed values for a single field. Useful when a choice field has many
options, or when a field is a *drill-down* (nested categories) that the
template can't show in full.

.. code-block:: text

   usage: paperpush options [-h] [-v] [-q] VENUE.FIELD [PATH ...]

Synopsis
--------

.. code-block:: bash

   paperpush options arxiv.crosslist_categories

Prints every valid value for the ``crosslist_categories`` field of the
``arxiv`` venue. Use the exact value(s) shown when filling the ``.sub`` file so
:doc:`validate` accepts them.

Arguments
---------

``VENUE.FIELD``
   The field to inspect, written as ``<venue>.<field>``, e.g.
   ``arxiv.crosslist_categories`` or ``nature.subject_level``.

``PATH`` (optional, repeatable)
   For a **drill-down** field whose options form a tree (for example
   ``nature.subject_level``), the category names to descend through before
   listing the next level. Each ``PATH`` element selects one level; the command
   then lists the choices available beneath it.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Examples
--------

List the top-level subject categories for Nature:

.. code-block:: bash

   paperpush options nature.subject_level

Descend into a category to see its sub-categories:

.. code-block:: bash

   paperpush options nature.subject_level "Biological sciences"

Descend two levels:

.. code-block:: bash

   paperpush options nature.subject_level "Biological sciences" "Genetics"

See also
--------

- :doc:`subfile` — the template already lists options inline for most fields;
  use ``options`` for long or nested lists.
- :doc:`validate` — checks that the values you chose are valid options.
