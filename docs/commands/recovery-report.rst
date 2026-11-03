``paperpush recovery-report``
=============================

Draft a bug report from a ``submit`` run whose steps needed recovery.

.. code-block:: text

   usage: paperpush recovery-report [-h] [-v] [-q] [--subfile SUB] [--json] [--open] [directory]

When a step of :doc:`submit` fails and is handled in the browser (by you with
``--recover by-hand``, by an agent through ``--recover agent`` or the MCP tools,
or by a model with ``--ai``), the run logs what failed and what was done to
``recoveries.jsonl`` in its recovery directory. An agent's or model's actions
are recorded exactly; for a fix by hand, the record is the one-line note you
were asked for. That usually means the portal changed and the venue script
is out of date for everyone, but on your machine nobody else ever sees the log.
This command turns it into a GitHub issue for the PaperPush maintainers, so the
venue script can be fixed in the next release.

``submit`` prints the exact command to run at the end of a run that needed
recovery.

Nothing is sent
---------------

The command prints the draft and a prefilled ``issues/new`` link. You read it,
and file it yourself if you are happy to share it. ``--open`` only opens that
page in your browser; you still review and submit it there.

The draft is redacted first:

- anything that was typed into the portal is replaced by its length;
- values from the run's ``.sub`` (names, e-mails, funders, titles, ...) are
  replaced by the name of the field they came from, e.g. ``<authors>``;
- other e-mail addresses, file paths (only the file name is kept), your home
  directory, and URL query strings (ScholarOne keeps session tokens there) are
  removed;
- screenshots are never included. The command lists where they are, so you can
  attach one by hand if it helps and you are happy to share it.

Arguments and options
---------------------

``directory``
   The run's recovery directory, as printed by ``submit``. Defaults to the most
   recent run under PaperPush's config directory that logged a recovery.

``--subfile SUB``
   The ``.sub`` the run used, whose values are masked. Defaults to the one the
   run recorded in ``run.json``.

``--json``
   Print the draft (title, body, issue URL, screenshot paths) as JSON.

``--open``
   Open the prefilled GitHub issue page in your browser.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

The draft is also saved as ``recovery-report.md`` in the recovery directory.
A long report is shortened in the link; paste the full text from that file.
