Quickstart
==========

PaperPush takes a manuscript from a directory of files to a filled-out
submission portal: it creates the manuscript metadata (``.sub``) file,
validates it against the venue's author guidelines, and fills out the
submission portal. Choose the path that suits you. Each works for any
:doc:`supported venue <venues>`.

For a real, end-to-end transcript of the agent flow (targeting
*Bioinformatics*), see :doc:`example-session`. There is also a `video tutorial
<https://youtu.be/ChCYoud0uBk>`_.

With an AI agent
----------------

Install the PaperPush skills (see :ref:`Installation <install-the-ai-agent-skills>`).
In Claude Code:

.. code-block:: text

   /plugin marketplace add pachterlab/paperpush
   /plugin install paperpush@paperpush

then ask:

.. code-block:: text

   /paperpush-prepare-submission Prepare my manuscript in /PATH/TO/MANUSCRIPT/DIRECTORY for submission to VENUE with PaperPush.

The skill runs the whole pipeline: it creates and autofills the ``.sub``,
validates it, logs in, and fills out the portal. Each stage also has its own
skill:

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Skill
     - Use it to
   * - ``/paperpush-prepare-submission``
     - go from a manuscript directory to a filled-in portal (autofill,
       validate, submit)
   * - ``/paperpush-autofill``
     - fill a ``.sub`` from the manuscript files
   * - ``/paperpush-validate``
     - run every check, including the ones that need a reader
   * - ``/paperpush-fix``
     - fix what validate flags (in the ``.tex``/``.docx`` sources and the
       ``.sub``), re-validate until nothing fixable is left, and report what
       needs the author
   * - ``/paperpush-submit``
     - log in and fill in the portal from a ready ``.sub`` (optionally
       skipping validation); invokes portal-debug if it stalls
   * - ``/paperpush-portal-debug``
     - get a stalled ``paperpush submit`` run unstuck
   * - ``/paperpush-add-venue-validate``
     - add or refresh a venue's author guidelines (contributors)
   * - ``/paperpush-add-venue``
     - run ``/paperpush-add-venue-validate`` and add ``login`` and ``submit``
       support for a venue, with tests (contributors)

Agents without skills should read ``AGENTS.md`` (``paperpush --agent-guide``
from a pip install), which describes how to run the pipeline while doing the
field extraction themselves.

On the website
--------------

1. Visit `paperpush.onrender.com <https://paperpush.onrender.com>`_.
2. Select the venue (or upload a ``.sub`` file).
3. Upload the manuscript files and fill out the metadata fields.
4. Click **Validate** to check the files and metadata against the author
   guidelines.
5. Click **Share** to share the editable form with co-authors (or download and
   share the ``.sub`` file).
6. If the venue supports it, click **Fill out submission portal
   automatically** and follow the instructions.

On the command line
-------------------

The examples use bioRxiv; replace ``biorxiv`` with any venue slug.

1. **Create a submission template**

   .. code-block:: bash

      paperpush subfile biorxiv

   This writes ``biorxiv.sub.txt``, a commented, YAML-like template listing
   every field the venue requires, with the allowed options for each choice
   field. Fields are grouped into the same sections for every venue
   (manuscript type, files, details, authors, reviewers, declarations,
   additional); add ``--venue-order`` to list them in the order the venue's
   portal asks for them instead. See :doc:`commands/subfile` and
   :ref:`what-is-a-sub-file`.

2. **Fill it in.** Any of:

   a. Edit ``biorxiv.sub.txt`` in a text editor, or in your browser with
      ``paperpush web biorxiv.sub.txt`` (see :doc:`commands/web`).

   b. Ask an AI agent with the ``/paperpush-autofill`` skill:
      ``/paperpush-autofill My manuscript files are in /PATH/TO/MANUSCRIPT/DIRECTORY. Fill out biorxiv.sub.txt``

   c. Use an LLM API (needs the provider's API key):

      .. code-block:: bash

         paperpush autofill --engine api -p /PATH/TO/MANUSCRIPT/DIRECTORY biorxiv.sub.txt

   The AI may not fill every field if some information is not in the
   manuscript directory. See :doc:`commands/autofill`.

3. **Validate**

   .. code-block:: bash

      paperpush validate biorxiv.sub.txt

   Runs the pre-submission checks on the filled ``.sub``: required fields,
   files, and options, plus the venue's author guidelines (section lengths,
   reference hallucination, figure resolution, link validation, anonymization,
   and more; see :doc:`validation-checks`). Add ``--ai`` (needs an API key),
   or use the ``/paperpush-validate`` skill, to also run the checks that need
   a reader's judgement. Use the ``/paperpush-fix`` skill to fix the issues
   validate finds: it edits the manuscript sources and the ``.sub``, validates
   again until nothing fixable is left, and reports what needs your decision.

   To check files before you have a ``.sub``, name them instead:

   .. code-block:: bash

      paperpush validate --manuscript main.tex --title-page title.tex --figures fig1.png fig2.png
      paperpush validate --manuscript main.tex --venue biorxiv   # also check the venue's guidelines

   See :doc:`commands/validate`.

4. **Share** ``biorxiv.sub.txt`` with co-authors. It can be read in a text
   editor, opened with ``paperpush web biorxiv.sub.txt``, or uploaded to
   `paperpush.onrender.com <https://paperpush.onrender.com>`_.

5. **Log in** to the venue's submission portal (venues with submit support
   only):

   .. code-block:: bash

      paperpush login biorxiv

   Credentials are stored in your OS keyring. See :doc:`commands/login`.

6. **Submit**: fill out the portal (venues with submit support only):

   .. code-block:: bash

      paperpush submit biorxiv.sub.txt

   PaperPush drives the venue's submission portal in a browser and stops
   **before** the final submit click. Review the form in the portal before
   submitting. Add ``--ai`` (or use the ``/paperpush-portal-debug`` skill) to
   have AI fix problems that come up, and ``--skip-validate`` to submit without
   passing validation. The ``/paperpush-submit`` skill runs login and submit
   together. See :doc:`commands/submit`.

Run the whole pipeline at once
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

From a clone of the repository, ``scripts/paperpush_pipeline.py`` runs
``subfile`` → ``autofill`` → ``validate`` → ``login`` → ``submit`` in sequence:

.. code-block:: bash

   python scripts/paperpush_pipeline.py -d /PATH/TO/MANUSCRIPT/DIRECTORY --engine api biorxiv

Run it with ``--help`` for the full set of options, grouped by step.

Helper commands
---------------

``paperpush venues``
   List supported venues. Filter with ``--validate``, ``--submit``,
   ``-k/--keywords``, and ``-t/--type`` (``preprint``, ``journal``,
   ``conference``), e.g. ``paperpush venues --submit -t journal -k biology``.
   See :doc:`commands/venues`.

``paperpush pick-venue``
   List supported venues that fit your manuscript's topic, from keywords, the
   manuscript file, or ``--title``/``--abstract``; add ``--ai`` for an LLM's
   ranking with reasons. It checks thematic fit only, not impact or likelihood
   of acceptance. See :doc:`commands/pick-venue`.

``paperpush update-venues``
   Update the venue data from the remote venue-data branch. See
   :doc:`commands/update-venues`.

``paperpush requirements VENUE``
   Print the venue's author guidelines. See :doc:`commands/requirements`.

``paperpush options VENUE.FIELD``
   Print the allowed values for a field in the venue's submission form. See
   :doc:`commands/options`.

``paperpush web VENUE.sub.txt``
   Open a ``.sub`` file in a local copy of the web app. See
   :doc:`commands/web`.

.. _what-is-a-sub-file:

What is a .sub file?
--------------------

The ``.sub`` file is a YAML-like text file holding all the metadata and file
paths needed to upload a manuscript to a venue's submission portal. In the
snippet below you would fill in the manuscript path, title, and abstract:

.. code-block:: yaml

   @venue: venue

   # Manuscript file (file, REQUIRED)
   #   Path to the submission.
   manuscript_file: /PATH/TO/MANUSCRIPT.pdf

   # Title (text, REQUIRED)
   #   The full title of the article.
   title: TITLE

   # Abstract (textarea, REQUIRED)
   #   Plain-text abstract, at least 20 characters
   abstract: |
     HERE IS THE ABSTRACT
