Quickstart
==========

This walkthrough takes a manuscript from a directory of files to a filled-out
submission portal. The example targets bioRxiv, but the same five steps work
for any :doc:`supported venue <venues>`.

For a real, end-to-end transcript of this flow (targeting *Bioinformatics*),
see :doc:`example-session`.

The five-step workflow
----------------------

1. **Create a submission template**

   .. code-block:: bash

      paperpush subfile biorxiv

   This writes ``biorxiv.sub`` — a commented, YAML-like template listing every
   field bioRxiv requires, with the allowed options for each choice field. See
   :doc:`commands/subfile`.

2. **Fill it in.** You have three options:

   a. Edit ``biorxiv.sub`` by hand.

   b. Ask an AI agent to fill it (the Claude skill ``/paperpush-autofill``, or
      any agent following ``AGENTS.md``).

   c. Use an LLM API:

      .. code-block:: bash

         paperpush autofill -d MANUSCRIPT_DIR --engine api biorxiv.sub

   See :doc:`commands/autofill`.

3. **Validate**

   .. code-block:: bash

      paperpush validate biorxiv.sub

   Checks for missing required fields, files that don't exist, and values that
   aren't valid options. See :doc:`commands/validate`.

4. **Log in** and store your credentials for the venue:

   .. code-block:: bash

      paperpush login biorxiv

   Credentials are stored in your OS keyring. See :doc:`commands/login`.

5. **Submit** — open the portal and fill it out:

   .. code-block:: bash

      paperpush submit biorxiv.sub

   PaperPush drives the venue's submission portal in a browser. It stops
   **before** the final submit click so you can review everything. See
   :doc:`commands/submit`.

Run the whole pipeline at once
------------------------------

``scripts/paperpush_pipeline.py`` runs ``subfile`` → ``autofill`` →
``validate`` → ``login`` → ``submit`` in sequence, taking you from a manuscript
directory to a filled portal in one command:

.. code-block:: bash

   python scripts/paperpush_pipeline.py -d /PATH/TO/MANUSCRIPT/DIRECTORY --engine api biorxiv

Run it with ``--help`` for the full set of options, grouped by step.

Using PaperPush with an AI agent
--------------------------------

If you use Claude Code, the simplest entry point is a single natural-language
request::

   Prepare my manuscript in PATH/TO/MANUSCRIPT/DIRECTORY for submission to
   VENUE with PaperPush.

The ``/paperpush-prepare-submission`` skill drives the full pipeline, and
``/paperpush-autofill`` handles just the field extraction. Both encode the same
contract described in ``AGENTS.md``.
