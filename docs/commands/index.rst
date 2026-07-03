Commands
========

The ``paperpush`` CLI is organised into subcommands, each covering one step of
the submission workflow. This section documents every command in detail.

.. code-block:: text

   usage: paperpush [-h] [--version] [--venues]
                    {subfile,options,autofill,validate,login,submit} ...

Global options
--------------

These apply to ``paperpush`` itself (before the subcommand):

``--version``
   Print the installed PaperPush version and exit.

``--venues``
   List every supported venue, grouped by type (preprint server, journal,
   conference), with its slug, then exit. See :doc:`../venues`.

Common options
--------------

Every subcommand accepts these logging flags:

``-v``, ``--verbose``
   Increase logging verbosity. ``-v`` enables info-level logging; ``-vv``
   enables debug. Overridden by the ``PAPERPUSH_LOG_LEVEL`` environment
   variable.

``-q``, ``--quiet``
   Log errors only.

The commands, in workflow order
-------------------------------

.. toctree::
   :maxdepth: 1

   subfile
   options
   autofill
   validate
   login
   submit
