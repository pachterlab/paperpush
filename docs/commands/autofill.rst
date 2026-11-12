``paperpush autofill``
======================

Fill a ``.sub`` file from a directory of manuscript files — the manuscript,
title page, figures, and supplements — so you don't have to type every field by
hand.

.. code-block:: text

   usage: paperpush autofill [-h] [-v] [-q] [-p PATH [PATH ...]]
                             [--engine {manual,api}] [--values FILE]
                             [--manuscript FILE] [--title-page FILE]
                             [--supplement FILE]
                             [--provider {anthropic,openai,google}]
                             [--model MODEL]
                             [--min-confidence {low,medium,high}]
                             [--confirm-attestations] [--keep-existing] [-o OUT]
                             [--overwrite] [--dry-run] [--web]
                             subfile

Synopsis
--------

.. code-block:: bash

   paperpush autofill -p MANUSCRIPT_DIR biorxiv.sub.txt

If the ``.sub`` file does not exist yet, ``autofill`` creates it first from the
venue slug in the filename (so ``biorxiv.sub.txt`` is templated as if you had run
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
   Extracts fields directly with an LLM API: Anthropic, OpenAI, or Google.
   Requires that provider's SDK (``pip install "paperpush[ai]"`` for all
   three, or ``paperpush[ai-anthropic]`` / ``[ai-openai]`` /
   ``[ai-google]`` for one) and its API key:

   =============  ==========================================  ====================
   Provider       API key variable                            Default model
   =============  ==========================================  ====================
   ``anthropic``  ``ANTHROPIC_API_KEY``                       ``claude-opus-5-5``
   ``openai``     ``OPENAI_API_KEY``                          ``gpt-6-astra``
   ``google``     ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``)  ``gemini-3.8-flash``
   =============  ==========================================  ====================

   Every provider gets the same prompt and the same output schema, and its
   answer goes through the same checks as the manual engine's.

Arguments
---------

``subfile``
   The ``.sub`` file to fill, e.g. ``biorxiv.sub.txt``. Created from the filename's
   venue slug if it does not already exist.

Options
-------

``-p PATH [PATH ...]``, ``--path PATH [PATH ...]`` (required)
   One or more manuscript directories and/or document files (``.tex``,
   ``.doc``, ``.docx``, ``.odt``, ``.rtf``, ``.txt``, ``.md``, ``.rst``,
   ``.pdf``) holding the manuscript, figures, and other files. Takes everything
   up to the next flag or the first ``.sub`` file, and may be repeated.
   (``-d``/``--directory`` are accepted as older names.)

``--engine {manual,api}``
   Which extraction engine to use. ``manual`` (default) reads values from
   ``--values``; ``api`` extracts them with an LLM API (see ``--provider``).

``--values FILE``
   JSON file of proposed field values. **Required for** ``--engine manual``.

``--manuscript FILE``
   *(api)* The manuscript file. Inferred from ``--path`` if omitted.

``--title-page FILE``
   *(api)* A standalone title page with author details, if it is separate from
   the manuscript.

``--supplement FILE``
   *(api)* A supplementary-materials file, if any.

``--provider {anthropic,openai,google}``
   *(api)* Which LLM API to use. Defaults to the first of ``anthropic``,
   ``openai``, ``google`` whose API key is set.

``--model MODEL``
   *(api)* The model to use. Defaults to the provider's model in the table
   above.

``--min-confidence {low,medium,high}``
   Do not write any value whose extraction confidence is below this threshold.
   Defaults to ``low`` (write everything).

``--confirm-attestations``
   Set every blank attestation to ``yes``. An attestation is a required yes/no
   confirmation whose only accepted answer is yes, such as author consent, the
   submission terms, or a code of ethics. The ``.sub`` marks each one
   ``must be yes to submit``. You are agreeing on behalf of every author, so the
   command prints each attestation's full sentence. An attestation already
   answered with something other than yes is left as written and reported.
   Without this flag, attestations are left blank for you, like every other
   policy field. AI agents pass it only when you ask them to.

``--keep-existing``
   Update an already-filled ``.sub`` without changing any value it holds:
   write only fields that are blank or still at their template default. Each
   field where the manuscript now says something different is listed under
   "Kept your existing value", showing both versions, so you can choose which
   to take. See `Updating a filled .sub`_.

``-o OUT``, ``--output OUT``
   Write the filled file here instead of overwriting the input ``.sub``.

``--overwrite``
   Overwrite ``--output`` if it already exists. (``--force`` is accepted as an
   older name.)

``--dry-run``
   Show what would be filled without writing anything.

``--web``
   Open the ``.sub`` in the paperpush web page on this computer to review the
   values, with every edit saved back to the file. With ``--values`` or
   ``--engine api`` the autofill runs here first. Otherwise the page runs it
   as soon as it opens, using the API key typed into the page or one set in
   the environment. The page also sets the manuscript field from the inputs if
   it is empty. Needs ``pip install 'paperpush[web]'``.

   Without ``--web``, the command still ends by printing a link to
   ``http://127.0.0.1:PORT/...`` that shows the ``.sub`` in the web page, served by
   a background process that stops after 30 minutes unused. It is printed only to
   a terminal with the web extra installed; set ``PAPERPUSH_NO_WEB_LINK=1`` to
   turn it off.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Examples
--------

Preview an API-based autofill without writing the file:

.. code-block:: bash

   paperpush autofill -p ./my_manuscript --engine api --dry-run biorxiv.sub.txt

Fill from an agent-produced ``values.json`` (the default manual engine):

.. code-block:: bash

   paperpush autofill -p ./my_manuscript --values values.json biorxiv.sub.txt

Use OpenAI even though an Anthropic key is also set:

.. code-block:: bash

   paperpush autofill -p ./my_manuscript --engine api --provider openai biorxiv.sub.txt

Write the result to a new file and only accept high-confidence values:

.. code-block:: bash

   paperpush autofill -p ./my_manuscript --engine api \
       --min-confidence high -o biorxiv.filled.sub.txt biorxiv.sub.txt

Updating a filled .sub
----------------------

Run ``autofill`` again on the same ``.sub`` after the manuscript changes. A
value the file already holds is reported as *unchanged* and not rewritten, and
a value that replaces an earlier one is marked ``[replaced the previous
value]``. To protect edits you made by hand, add ``--keep-existing`` (with
``--dry-run`` to preview first):

.. code-block:: bash

   paperpush autofill -p ./my_manuscript --engine api --keep-existing --dry-run biorxiv.sub.txt

Then update the fields you want from the new draft, either by editing the
``.sub`` or by rerunning without ``--keep-existing`` with a ``values.json`` that
holds just those fields. The ``/paperpush-autofill`` skill does this for you:
it reads the venue and file paths from the existing ``.sub`` and asks you about
each field the new draft would change.

.. note::

   ``autofill`` gets you most of the way there, but always review the result
   and run :doc:`validate` before submitting. Author lists, affiliations, and
   funding details are the fields most worth double-checking.

See also
--------

- :doc:`validate` — verify the filled file before submitting.
- ``AGENTS.md`` in the repo — the contract AI agents follow to produce
  ``values.json`` for the manual engine.
