``paperpush submit``
====================

Open the venue's submission portal in a browser and run the submission
click-through, filling out the form from your ``.sub`` file.

.. code-block:: text

   usage: paperpush submit [-h] [-v] [-q] [--headless] [--debug] [--new-session]
                           [--timeout SECONDS]
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

``--timeout SECONDS``
   Cap how long the browser waits for any action or page load before failing.
   Default: ``10`` seconds; ``0`` waits forever. Increase it on a slow
   connection or a sluggish portal.

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
Use ``paperpush --venues`` (or :doc:`../venues`) to find the slug — and the
matching sample directory — for your target venue.

Examples
--------

Fill the portal, headed, so you can review before submitting:

.. code-block:: bash

   paperpush submit biorxiv.sub

Step through the wizard with the Playwright Inspector:

.. code-block:: bash

   paperpush submit biorxiv.sub --debug

Sign in fresh after switching accounts, with a longer timeout:

.. code-block:: bash

   paperpush submit biorxiv.sub --new-session --timeout 30

See also
--------

- :doc:`login` — store credentials before submitting.
- :doc:`validate` — always run this first.
- :doc:`../example-session` — a full portal walkthrough transcript.
