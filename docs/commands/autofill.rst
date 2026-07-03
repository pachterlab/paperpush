``paperpush autofill``
======================

Fill a ``.sub`` file from a directory of manuscript files — the manuscript,
title page, figures, and supplements — so you don't have to type every field by
hand.

.. code-block:: text

   usage: paperpush autofill [-h] [-v] [-q] -d MANUSCRIPTDIR
                             [--engine {manual,api}] [--values FILE]
                             [--manuscript FILE] [--title-page FILE]
                             [--supplement FILE] [--model MODEL]
                             [--min-confidence {low,medium,high}] [-o OUTPUT]
                             [--force] [--dry-run]
                             subfile

Synopsis
--------

.. code-block:: bash

   paperpush autofill -d MANUSCRIPT_DIR biorxiv.sub

If the ``.sub`` file does not exist yet, ``autofill`` creates it first from the
venue slug in the filename (so ``biorxiv.sub`` is templated as if you had run
``paperpush subfile biorxiv``).

Engines
-------

``autofill`` supports two field-extraction engines:

``manual`` (default)
   Reads proposed field values from a JSON file passed with ``--values`` and
   writes them into the ``.sub``. The extraction of those values is done
   *outside* PaperPush — this is the path the Claude ``/paperpush-autofill``
   skill and other AI agents use: the agent reads the manuscript, writes a
   ``values.json``, and hands it to the deterministic core.

``api``
   Extracts fields directly with the Anthropic API. Requires the ``autofill``
   extra (``pip install "paperpush[autofill]"``) and an Anthropic API key.

Arguments
---------

``subfile``
   The ``.sub`` file to fill, e.g. ``biorxiv.sub``. Created from the filename's
   venue slug if it does not already exist.

Options
-------

``-d MANUSCRIPTDIR``, ``--directory MANUSCRIPTDIR`` (required)
   Directory holding the manuscript, figures, and other files.

``--engine {manual,api}``
   Which extraction engine to use. ``manual`` (default) reads values from
   ``--values``; ``api`` extracts them with the Anthropic API.

``--values FILE``
   JSON file of proposed field values. **Required for** ``--engine manual``.

``--manuscript FILE``
   *(api)* The manuscript file. Inferred from ``--directory`` if omitted.

``--title-page FILE``
   *(api)* A standalone title page with author details, if it is separate from
   the manuscript.

``--supplement FILE``
   *(api)* A supplementary-materials file, if any.

``--model MODEL``
   *(api)* The Anthropic model to use. Defaults to ``claude-opus-4-8``.

``--min-confidence {low,medium,high}``
   Do not write any value whose extraction confidence is below this threshold.
   Defaults to ``low`` (write everything).

``-o OUTPUT``, ``--output OUTPUT``
   Write the filled file here instead of overwriting the input ``.sub``.

``--force``
   Overwrite ``--output`` if it already exists.

``--dry-run``
   Show what would be filled without writing anything.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Examples
--------

Preview an API-based autofill without writing the file:

.. code-block:: bash

   paperpush autofill -d ./my_manuscript --engine api --dry-run biorxiv.sub

Fill from an agent-produced ``values.json`` (the default manual engine):

.. code-block:: bash

   paperpush autofill -d ./my_manuscript --values values.json biorxiv.sub

Write the result to a new file and only accept high-confidence values:

.. code-block:: bash

   paperpush autofill -d ./my_manuscript --engine api \
       --min-confidence high -o biorxiv.filled.sub biorxiv.sub

.. note::

   ``autofill`` gets you most of the way there, but always review the result
   and run :doc:`validate` before submitting. Author lists, affiliations, and
   funding details are the fields most worth double-checking.

See also
--------

- :doc:`validate` — verify the filled file before submitting.
- ``AGENTS.md`` in the repo — the contract AI agents follow to produce
  ``values.json`` for the manual engine.
