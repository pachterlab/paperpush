Quickstart
==========

Choose your favorite mode: AI agent skills, website, or command line. All
modes support venue-specific metadata collection, venue-tailored checking, and
submission portal filling for :doc:`supported venues <venues>`. AI agents
support additional tasks.

.. list-table::
   :header-rows: 1
   :widths: 22 39 39

   * - Mode
     - Best for
     - What you need
   * - :doc:`quickstart/ai`
     - The whole flow from a manuscript directory, plus choosing a venue,
       fixing validation issues, transferring between venues, and responding
       to reviewers
     - Claude Code, Codex, or another agent with the PaperPush skills (or an
       MCP client)
   * - :doc:`quickstart/web`
     - Filling in and checking a submission with no install, and sharing the
       form with co-authors
     - A browser: `paperpush.onrender.com <https://paperpush.onrender.com>`_
   * - :doc:`quickstart/cli`
     - Scripted, reproducible runs of each step
     - ``pip install "paperpush[all]"`` and ``playwright install chromium``

The modes share the same ``.sub`` file (see :doc:`sub-file`), so you can mix
them: create and fill a ``.sub`` with an agent, review it on the website with
co-authors, and submit it from the command line.

.. note::

   PaperPush never presses the final submit button. It brings you to one of
   the last steps of the venue's portal; review the form there before
   submitting. ``submit`` is for **initial submissions only**: upload a
   revision through the venue's own "submit revision" link for the existing
   manuscript (prepare it with ``/paperpush-respond-to-reviewers``).

For a real, end-to-end transcript of the agent flow (targeting
*Bioinformatics*), see :doc:`example-session`. There is also a `video tutorial
<https://youtu.be/ChCYoud0uBk>`_.

.. toctree::
   :maxdepth: 1

   quickstart/ai
   quickstart/web
   quickstart/cli
