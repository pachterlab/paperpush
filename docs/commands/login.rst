``paperpush login``
===================

Store your credentials for a venue's submission system so :doc:`submit` can sign
in on your behalf. Credentials are kept in your operating system's keyring, not
in the ``.sub`` file.

.. code-block:: text

   usage: paperpush login [-h] [-v] [-q] [--list] [-u USERNAME]
                          [--password PASSWORD] [--confirm-password] [--orcid]
                          [--orcid-id ID] [--into SUBFILE] [--status]
                          [--logout] [--no-verify] [--verify-headless]
                          [--timeout SECONDS]
                          [venue]

Synopsis
--------

.. code-block:: bash

   paperpush login biorxiv

By default this prompts for your username and password, then **opens a browser
and verifies them** against the venue's sign-in page before saving. Verifying up
front means ``submit`` won't fail later on a bad password.

Arguments
---------

``venue``
   The venue slug, e.g. ``biorxiv``. Omit it only when using ``--list``.

Options
-------

Credential entry
~~~~~~~~~~~~~~~~~

``-u USERNAME``, ``--username USERNAME``
   Username or email. If omitted, you are prompted, or the
   ``PAPERPUSH_USERNAME`` environment variable is used.

``--password PASSWORD``
   Password. If omitted, you are prompted, or the ``PAPERPUSH_PASSWORD``
   environment variable is used.

   .. warning::

      Passing ``--password`` on the command line exposes it in plain text in
      your shell history and the system process list. Prefer being prompted, or
      the ``PAPERPUSH_PASSWORD`` environment variable.

``--confirm-password``
   Ask for the password a second time and check the two match. Off by default:
   the prompt asks once, and a typo is caught by the sign-in check anyway. Has
   no effect when the password comes from ``--password`` or the environment.

Sign in with ORCID
~~~~~~~~~~~~~~~~~~~

Many journal submission systems offer a "Sign in with ORCID" button beside their
own login form. ``--orcid`` stores the credential that button needs, and works
exactly like the username/password path: you are prompted for your **ORCID iD**
and your **ORCID password**, they are checked against the venue's real sign-in
page (down its ORCID branch), and then stored. ``submit`` later signs in the same
way.

.. code-block:: text

   $ paperpush login biorxiv --orcid
   ORCID iD or email: 0000-0002-1825-0097
   ORCID password:
   Checking the ORCID credentials by signing in to biorxiv…

Journals and preprint servers both offer it; conference portals run on their own
accounts, so ``--orcid`` is refused there (as it is for Discrete Mathematics,
whose portal has no ORCID control). Driving the hand-off has so far been recorded
for bioRxiv, medRxiv, arXiv, every Editorial Manager journal (Cell, Cell Systems,
Cell Genomics, PLOS Computational Biology), BMC Bioinformatics, and Genome
Biology. Asking any other venue for an ORCID sign-in reports that it is not
implemented yet and stores nothing, so use a username and password there.

paperpush is not a registered ORCID API client and runs no OAuth flow: the
browser types your ORCID password into ORCID's own sign-in page, exactly as you
would by hand -- in a popup window (Editorial Manager) or the same tab (bioRxiv,
medRxiv, arXiv), whichever that portal opens.

``--orcid``
   Sign in with your ORCID account instead of a venue username and password.

``--orcid-id ID``
   Your ORCID iD (e.g. ``0000-0002-1825-0097``), or the email registered with
   ORCID. Implies ``--orcid`` and skips that prompt; you are still asked for the
   ORCID password. Also read from the ``PAPERPUSH_ORCID_ID`` environment
   variable.

``--into SUBFILE``
   After an ORCID login, read the author's *public* ORCID record and fill their
   ORCID / email / affiliation into the matching author line of this ``.sub``
   file. A convenience, not part of signing in — it needs an iD rather than an
   email, and a failed lookup does not fail the login.

Manage stored credentials
~~~~~~~~~~~~~~~~~~~~~~~~~~~

``--list``
   List the venues you are logged in to, with the username or ORCID iD each
   login belongs to, then exit. (Use without a ``venue`` argument.)

``--status``
   Show whether credentials are stored for the venue, then exit.

``--logout``
   Remove the stored credentials for the venue.

Verification behaviour
~~~~~~~~~~~~~~~~~~~~~~~~

``--no-verify``
   Store the credentials without checking them against the venue's sign-in. By
   default, login opens a browser and verifies first.

``--verify-headless``
   Run the verification browser headless (no window). Note: a headless browser
   cannot complete a CAPTCHA or two-factor prompt.

``--timeout SECONDS``
   Cap how long the verification browser waits for any action or page load
   before failing. Default: ``10`` seconds; ``0`` waits forever.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Examples
--------

Log in and verify interactively:

.. code-block:: bash

   paperpush login biorxiv

Check what you're logged in to:

.. code-block:: bash

   paperpush login --list

Check status for one venue:

.. code-block:: bash

   paperpush login biorxiv --status

Sign in with ORCID (you are prompted for your ORCID password):

.. code-block:: bash

   paperpush login cell_genomics --orcid

Same, backfilling your author details into a ``.sub`` from your public record:

.. code-block:: bash

   paperpush login cell_genomics --orcid-id 0000-0002-1825-0097 --into cell_genomics.sub

Remove stored credentials:

.. code-block:: bash

   paperpush login biorxiv --logout

See also
--------

- :doc:`submit` — uses the credentials stored here to sign in to the portal.
