``paperpush prepare-submission``
================================

Run the whole workflow in one command: :doc:`subfile`, :doc:`autofill` (through
an LLM API), :doc:`validate`, :doc:`login`, and :doc:`submit`. ``prepare`` is a
shorter name for it.

.. code-block:: text

   usage: paperpush prepare-submission [-h] [-v] [-q] -p PATH [PATH ...] [--sub FILE] [--overwrite] [--refill]
                                       [--manuscript FILE] [--title-page FILE] [--supplement FILE]
                                       [--provider {anthropic,openai,google}] [--model MODEL] [--ai-checks]
                                       [--no-submit] [-y] [--recover {off,by-hand,ai}] [--timeout SECONDS]
                                       [venue]

.. code-block:: bash

   paperpush prepare-submission biorxiv -p ~/papers/my-paper
   paperpush prepare-submission -p ~/papers/my-paper biorxiv   # the venue may also come last

The venue must be one paperpush supports (``paperpush venues``); ``general`` is
refused, since there is nothing to submit to.

It is the command-line counterpart of the ``paperpush-prepare-submission``
agent skill. The skill has the agent read the manuscript itself; this command
needs an API key (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, or
``GEMINI_API_KEY``), and checks for it before writing anything.

The steps
---------

1. **Submission file.** Uses ``--sub`` if given, else ``VENUE.sub.txt`` in the
   manuscript directory if it exists, else creates it there.
2. **Autofill.** Fills a new ``.sub`` with ``autofill --engine api``. A ``.sub``
   that already existed is left as it is, so running the command again after
   fixing the file keeps your fixes; ``--refill`` autofills it again (replacing
   the values the model extracts) and ``--overwrite`` starts from a fresh
   template.
3. **Review.** In a terminal, it pauses so you can check the model's values in
   your editor or with ``paperpush web``, and asks before validating.
4. **Validate.** Prints the full report. Any error stops the run. More than 5
   warnings also stops it, unless you confirm or pass ``--yes``.
   ``--ai-checks`` runs the checks that need AI through the same API (as
   ``validate --ai``). Takes validate's ``--disable-CHECK``/``--enable-CHECK``,
   ``--anonymous``, and ``--initial-submission`` flags.
5. **Login.** Re-checks a stored login by signing in, and offers to sign in
   again if that fails. With no stored login, it starts ``paperpush login`` in a
   terminal, and stops without one.
6. **Submit.** Runs :doc:`submit` without validating again. As always, it stops
   before the final submit button.

For a validate-only venue (no portal runner), or with ``--no-submit``, it stops after validation.

Options
-------

``-p``, ``--path PATH ...``
   (Required) the manuscript directory and/or document files.
``--sub FILE``
   The ``.sub`` to use or create.
``--overwrite`` / ``--refill``
   Start from a fresh template / autofill an existing ``.sub`` again.
``--manuscript``, ``--title-page``, ``--supplement``
   Name the documents instead of letting autofill find them.
``--provider``, ``--model``
   The LLM API to use, as for ``autofill --engine api``.
``--ai-checks``
   Also run validate's AI checks through the API.
``--no-submit``
   Stop after validation.
``-y``, ``--yes``
   Don't stop to ask: skip the review pause, carry on despite more than 5
   warnings, and open the portal without a terminal.
``--recover``, ``--timeout``
   Passed to :doc:`submit`.
