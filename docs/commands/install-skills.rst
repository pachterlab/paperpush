``paperpush install-skills``
============================

Install paperpush's agent skills, so an AI agent knows how to autofill,
validate, and submit with paperpush.

.. code-block:: text

   usage: paperpush install-skills [-h] [-v] [-q] [--agent {claude,codex}]
                                   [--project [DIR] | --dest DIR] [--all]
                                   [--overwrite] [--list | --uninstall]
                                   [SKILL ...]

A skill is a set of instructions that an agent (Claude Code, Codex, ...) loads
when your request matches it. The skills ship with paperpush, and this command
copies them into the agent's skills directory. Run it again after upgrading
paperpush to update the installed copies. Start a new agent session to load
them.

=================================  ==============================================================
Skill                              Use it to
=================================  ==============================================================
``paperpush-prepare-submission``   go from a manuscript directory to a filled-in portal
``paperpush-autofill``             fill a ``.sub`` from the manuscript files
``paperpush-validate``             run every check, including the ones that need a reader
``paperpush-submit``               log in and fill in the portal from a ready ``.sub``
``paperpush-portal-debug``         get a stalled ``paperpush submit`` run unstuck
``paperpush-add-venue-validate``   add or refresh a venue's author guidelines (contributor)
``paperpush-add-venue``            add ``submit`` support for a venue, with tests (contributor)
=================================  ==============================================================

The contributor skills edit paperpush itself and need a clone of the
repository, so they are installed only with ``--all`` or by name. ``AGENTS.md``
(``paperpush --agent-guide``) says when an agent should use each skill.

Claude Code and Codex can also install the skills as a plugin, with no
paperpush install needed for the skills themselves:

.. code-block:: text

   /plugin marketplace add pachterlab/paperpush         # Claude Code
   /plugin install paperpush@paperpush

   codex plugin marketplace add pachterlab/paperpush    # Codex
   codex plugin add paperpush@paperpush

Arguments
---------

``SKILL``
   Install only these skills, e.g. ``paperpush-autofill`` or just ``autofill``.
   Default: every author skill.

Options
-------

``--agent {claude,codex}``
   The agent to install for. ``claude`` (default) installs into
   ``~/.claude/skills``; ``codex`` into ``~/.codex/skills``.

``--project [DIR]``
   Install into the project at ``DIR`` (default: the current directory), e.g.
   ``DIR/.claude/skills``, instead of your user skills directory.

``--dest DIR``
   Install into this skills directory, for any other agent.

``--all``
   Also install the contributor skills.

``--overwrite``
   Replace a skill directory of the same name that this command did not
   install. Without it, such a directory is left alone and the command exits 1.

``--list``
   List the skills, with whether each is installed in the target directory.

``--uninstall``
   Remove the skills this command installed. Directories it did not install
   are kept.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

Examples
--------

.. code-block:: console

   $ paperpush install-skills
   installed paperpush-autofill
   installed paperpush-portal-debug
   installed paperpush-prepare-submission
   installed paperpush-submit
   installed paperpush-validate
   Skills directory: /home/me/.claude/skills
   Contributor skills not installed (paperpush-add-venue, paperpush-add-venue-validate); add --all if you work on paperpush itself.
   Start a new agent session to load them.

   $ paperpush install-skills --agent codex --project .
   $ paperpush install-skills --uninstall
