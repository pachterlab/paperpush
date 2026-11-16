``paperpush login``
===================

Store your credentials for a venue's submission system so :doc:`submit` can sign
in on your behalf. Credentials are kept in your operating system's keyring, not
in the ``.sub`` file.

.. code-block:: text

   usage: paperpush login [-h] [-v] [-q] [--list] [-u USERNAME]
                          [--password PASSWORD] [--confirm-password] [--orcid] [--github]
                          [--provider NAME] [--orcid-id ID] [--into SUBFILE] [--status] [--check]
                          [--logout] [--no-verify] [--verify-headless]
                          [--timeout SECONDS]
                          [venue]

Synopsis
--------

.. code-block:: bash

   paperpush login biorxiv

Zenodo supports these three sign-in methods:

.. code-block:: bash

   paperpush login zenodo           # Zenodo email and password
   paperpush login zenodo --orcid   # ORCID iD/email and ORCID password
   paperpush login zenodo --github  # GitHub username/email and GitHub password

Run the command in your own terminal. Provider credentials are entered only on
the provider's sign-in page. Complete any two-factor, authorization, or account
linking prompts in the open browser, then return to the terminal. The saved
login remembers the selected method for subsequent sign-ins and ``--check``.

NCBI services (``sra``, ``geo``) sign in through NCBI's account page, which
offers several identity providers; ``--provider`` picks the one your account
uses:

.. code-block:: bash

   paperpush login sra                      # NCBI account username and password
   paperpush login sra --provider google    # Google account
   paperpush login geo --provider era       # eRA Commons
   paperpush login geo --orcid              # ORCiD (same as --provider orcid)

GEO is login-only for now: the sign-in and saved session work, but
``paperpush submit`` has no runner for its portal yet. SRA supports both.

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
Cell Genomics, PLOS Computational Biology), BMC Bioinformatics, Genome Biology,
and Zenodo. Asking any other venue for an ORCID sign-in reports that it is not
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

Sign in with GitHub
~~~~~~~~~~~~~~~~~~~

``--github``
   Sign in through the venue's "Sign in with GitHub" button using your GitHub
   username/email and GitHub password. Currently supported for Zenodo. Cannot
   be combined with ``--orcid`` or ``--orcid-id``. Verification, secure storage,
   and later reuse follow the same flow as email login. MFA, authorization, and
   account linking are completed by the author in the browser.

Choose an identity provider
~~~~~~~~~~~~~~~~~~~~~~~~~~~

``--provider NAME``
   For a venue whose sign-in page offers several identity providers, which one
   the username and password belong to. The NCBI venues (``sra``, ``geo``) take
   ``ncbi`` (the default), ``era`` (eRA Commons), ``google``, ``orcid``,
   ``login.gov``, ``microsoft`` and ``nih``; see :doc:`../ncbi-login`. The
   choice is stored with the credential, so ``submit`` and ``--check`` sign in
   the same way. Cannot be combined with ``--orcid`` or ``--github``. Two-factor,
   CAPTCHA, PIV and account-linking steps are finished by hand in the
   verification browser, so keep it headed (no ``--verify-headless``) for
   providers that always ask for them, such as Login.gov.

Manage stored credentials
~~~~~~~~~~~~~~~~~~~~~~~~~~~

``--list``
   List the venues you are logged in to, with the username or ORCID iD each
   login belongs to, then exit. (Use without a ``venue`` argument.)

``--status``
   Show whether credentials are stored for the venue, then exit.

``--check``
   Sign in with the stored credentials, in a headless browser, to confirm they
   still work, then exit. Unlike ``--status``, this catches a password changed
   since you logged in. Exits ``1`` if nothing is stored or the sign-in fails;
   re-run ``paperpush login <venue>`` to fix it. A headless browser cannot
   complete a CAPTCHA or two-factor prompt, so a venue that always asks for one
   fails this check. A venue paperpush cannot sign in to automatically prints a
   note and exits ``0``.

``--logout``
   Remove the stored credentials for the venue.

Verification behavior
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

Confirm the stored login still works before submitting:

.. code-block:: bash

   paperpush login biorxiv --check

Sign in with ORCID (you are prompted for your ORCID password):

.. code-block:: bash

   paperpush login cell_genomics --orcid

Same, backfilling your author details into a ``.sub`` from your public record:

.. code-block:: bash

   paperpush login cell_genomics --orcid-id 0000-0002-1825-0097 --into cell_genomics.sub.txt

Remove stored credentials:

.. code-block:: bash

   paperpush login biorxiv --logout

See also
--------

- :doc:`submit` — uses the credentials stored here to sign in to the portal.
