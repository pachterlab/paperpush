``paperpush login``
===================

Store your credentials for a venue's submission system so :doc:`submit` can sign
in on your behalf. Credentials are kept in your operating system's keyring, not
in the ``.sub`` file.

.. code-block:: text

   usage: paperpush login [-h] [-v] [-q] [--list] [-u USERNAME]
                          [--password PASSWORD] [--orcid] [--orcid-id ID]
                          [--into SUBFILE] [--status] [--logout] [--no-verify]
                          [--verify-headless] [--timeout SECONDS]
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

Sign in with ORCID
~~~~~~~~~~~~~~~~~~~

``--orcid``
   Sign in with ORCID instead of a username and password.

``--orcid-id ID``
   Your ORCID iD (e.g. ``0000-0002-1825-0097``). Implies ``--orcid`` and skips
   the prompt.

``--into SUBFILE``
   After an ORCID login, fill the matching author's ORCID / name / affiliation
   into this ``.sub`` file.

Manage stored credentials
~~~~~~~~~~~~~~~~~~~~~~~~~~~

``--list``
   List the venues you are logged in to, with usernames, then exit. (Use
   without a ``venue`` argument.)

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

Sign in with ORCID and backfill your author details into a ``.sub``:

.. code-block:: bash

   paperpush login biorxiv --orcid-id 0000-0002-1825-0097 --into biorxiv.sub

Remove stored credentials:

.. code-block:: bash

   paperpush login biorxiv --logout

See also
--------

- :doc:`submit` — uses the credentials stored here to sign in to the portal.
