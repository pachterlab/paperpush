Commands
========

The ``paperpush`` CLI is organised into subcommands, each covering one step of
the submission workflow. This section documents every command in detail.

.. code-block:: text

   usage: paperpush [-h] [--version] [--agent-guide]
                    {venues,subfile,options,autofill,validate,requirements,login,submit,codegen,recovery-report,web,pick-venue,add-venue,update-venues,install-skills} ...

Global options
--------------

These apply to ``paperpush`` itself (before the subcommand):

``--version``
   Print the installed PaperPush version and exit.

``--agent-guide``
   Print the guide for AI agents driving the CLI (``AGENTS.md``) and exit.

To list the supported venues, use the :doc:`venues` command.

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

   venues
   subfile
   options
   autofill
   validate
   requirements
   login
   submit
   recovery-report
   codegen
   web
   pick-venue
   add-venue
   update-venues
   install-skills
