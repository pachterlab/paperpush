``paperpush submit``
====================

Open the venue's submission portal in a browser and run the submission
click-through, filling out the form from your ``.sub`` file.

.. code-block:: text

   usage: paperpush submit [-h] [-v] [-q] [--headless] [--debug] [--new-session]
                           [--timeout SECONDS] [--close-on-failure]
                           [--recover {off,by-hand,ai,agent}] [--ai]
                           [--provider {anthropic,openai,google}]
                           [--model MODEL] [--recover-dir DIR]
                           [--cdp-port PORT]
                           subfile

.. important::

   ``submit`` fills out the portal but **stops before the final submit
   button**. Nothing is submitted to the venue automatically. Review the form
   in the browser and click submit yourself.

Synopsis
--------

.. code-block:: bash

   paperpush submit biorxiv.sub

By default the browser runs **headed** (a visible window) so you can complete
sign-in, solve any CAPTCHA or two-factor prompt, and review each page as it is
filled. PaperPush uses the credentials stored by :doc:`login` and reuses a
saved browser session across runs where possible.

Before you run it, make sure you have:

1. A filled ``.sub`` file that passes :doc:`validate`.
2. Stored credentials for the venue (:doc:`login`).

Arguments
---------

``subfile``
   Path to the ``.sub`` file to submit, e.g. ``biorxiv.sub``.

Options
-------

``--headless``
   Run the browser headless (no window). Default is headed so you can sign in
   and review. A headless run cannot complete a CAPTCHA or two-factor prompt.

``--debug``
   Open the Playwright Inspector at the first step so you can step through the
   submission wizard one action at a time. Reuses a saved session from an
   earlier run to skip sign-in.

``--new-session``
   Discard any saved browser session and sign in fresh. Use this after
   switching accounts.

``--skip-validate``
   Do not run :doc:`validate` on the ``.sub`` before opening the browser. By
   default ``submit`` validates first and stops on any error; when it does, the
   error message suggests this flag. Use it when you have checked the errors
   and want to submit anyway.

``--timeout SECONDS``
   Cap how long the browser waits for any action or page load before failing.
   Default: ``10`` seconds; ``0`` waits forever. Increase it on a slow
   connection or a sluggish portal.

``--close-on-failure``
   Close the browser when the run fails. By default the window stays open at
   the step that broke so you can see the page and finish by hand.

``--recover {off,by-hand,ai,agent}``
   What to do when one step of the click-through fails, for example when the
   portal has renamed a button. ``off`` (the default) stops the run.
   ``by-hand`` pauses and asks in the terminal: fix the page in the browser
   (or do the step yourself), then press Enter to continue from the next
   step, ``r`` to retry the step, or ``a`` to abort. After a fix you are asked,
   optionally, what you changed; that line and the failed step are logged (see
   ``--recover-dir``), since clicks in the browser cannot be. ``ai`` is the same as
   ``--ai`` (below). ``agent`` pauses for an
   AI agent instead. The MCP server's ``submit`` tool uses it, with the agent
   working through ``submit_page``, ``submit_act`` and ``submit_resume``. Every
   venue's runner supports recovery. Sign-in is never part of it, since a
   password, CAPTCHA or two-factor prompt is yours to handle.

``--ai``
   When a step fails, send the page (its accessibility tree and a screenshot,
   which can show your manuscript's details) to an LLM. The model fixes the step
   in the browser: it clicks, fills, selects and uploads, then checks the
   result, and the run carries on from the next step. The model can never send
   the submission. Clicks on ``Submit``/``Approve`` controls, the Enter key,
   uploads of files the ``.sub`` does not name, navigation and scripts are all
   refused. It gets at most 25 model calls per failed step and 5 failed steps
   per run. In a terminal with a visible browser, a step the model gives up on
   is handed to you, as with ``by-hand``. Uses the same API keys as
   ``autofill --engine api`` (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``,
   ``GEMINI_API_KEY``/``GOOGLE_API_KEY``) and needs that provider's SDK
   (``pip install "paperpush[ai]"``).

``--provider {anthropic,openai,google}`` / ``--model MODEL``
   With ``--ai``: which provider and model to use. They default to the first
   provider with an API key set, and to that provider's default model, as for
   ``autofill``.

``--recover-dir DIR``
   With ``--recover agent``, ``--recover by-hand`` or ``--ai``, where the run
   keeps its recovery log (and, for ``agent``, the files the paused run and the
   agent exchange). Defaults to a fresh one under PaperPush's config
   directory, created only when a step fails. It also collects ``recoveries.jsonl``, a record of each failed
   step and what fixed it, and ``run.json`` (the venue and PaperPush version).

   Within one run, a fix is reused. When the same action on the same element
   fails again and the earlier fix only waited or clicked a named button or
   link, it is repeated without pausing. A fix that typed, selected or uploaded
   something is never repeated, since on a repeated widget (the second funder,
   the second upload row) it would enter the previous entry's data. Instead the
   agent or model is shown it as ``earlier_fix``, to adapt.

   A run that needed recovery ends by printing a
   :doc:`recovery-report <recovery-report>` command: it drafts a redacted bug
   report you can choose to file, so the venue gets fixed for everyone.

``--cdp-port PORT``
   Open a Chrome DevTools Protocol endpoint on ``127.0.0.1:PORT`` (``0`` picks
   a free port) so another tool can drive the same browser, for example
   ``playwright-cli attach --cdp=http://127.0.0.1:PORT``. While the run is
   open, anything on your machine can then control the signed-in browser, and
   the agent guardrails (no final submit, uploads limited to ``.sub`` files)
   do not apply to it.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags. Use ``-vv``
to see each portal step as it runs — helpful when a venue changes its form.

Sample ``.sub`` files
---------------------

The repository ships a complete, filled example for **every supported venue**
under:

.. code-block:: text

   tests/sub_files/<venue>/<venue>.sub

For example, ``tests/sub_files/biorxiv/biorxiv.sub``,
``tests/sub_files/nature/nature.sub``, or
``tests/sub_files/cell/cell.sub``. Some venues include extra variants
alongside the main file — for instance
``tests/sub_files/biorxiv/biorxiv_figures.sub`` and
``biorxiv_supplement.sub`` — showing how figure lists, supplements, and funding
fields are laid out.

These are the best reference for what a correctly filled file looks like: copy
the one for your venue, swap in your own values, and adjust the file paths.
Use ``paperpush venues`` (or :doc:`../venues`) to find the slug — and the
matching sample directory — for your target venue.

Examples
--------

Fill the portal, headed, so you can review before submitting:

.. code-block:: bash

   paperpush submit biorxiv.sub

Step through the wizard with the Playwright Inspector:

.. code-block:: bash

   paperpush submit biorxiv.sub --debug

Have an LLM fix any step the portal has changed, using your OpenAI key:

.. code-block:: bash

   paperpush submit biorxiv.sub --ai --provider openai

Pause on a failed step so you can fix it in the browser and carry on:

.. code-block:: bash

   paperpush submit biorxiv.sub --recover by-hand

Sign in fresh after switching accounts, with a longer timeout:

.. code-block:: bash

   paperpush submit biorxiv.sub --new-session --timeout 30

See also
--------

- :doc:`login` — store credentials before submitting.
- :doc:`validate` — always run this first.
- :doc:`../example-session` — a full portal walkthrough transcript.
