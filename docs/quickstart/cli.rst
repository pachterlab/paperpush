Command line interface
======================

Install
-------

.. code-block:: bash

   pip install "paperpush[all]"   # recommended: AI API usage, MCP server, local web deployment, and additional validation checks
   playwright install chromium    # required for submit: the Playwright browser

``pip install paperpush`` is enough for basic CLI use. See :doc:`../installation`
for the optional extras.

Venue-specific metadata collection, venue-tailored checking, and submission portal filling for a supported venue
----------------------------------------------------------------------------------------------------------------

The examples use bioRxiv; replace ``biorxiv`` with any venue slug
(``paperpush venues`` lists them). The agent skill
``/paperpush-prepare-submission`` runs this entire pipeline.

1. **Create a .sub file**

   .. code-block:: bash

      paperpush subfile biorxiv

   This writes ``biorxiv.sub.txt``, a commented, YAML-like template listing
   every field the venue requires, with the allowed options for each choice
   field. Fields are grouped into the same sections for every venue
   (manuscript type, files, details, authors, reviewers, declarations,
   additional); add ``--venue-order`` to list them in the order the venue's
   portal asks for them instead. Run ``paperpush subfile general`` if you
   have not chosen a venue yet. See :doc:`../commands/subfile` and
   :doc:`../sub-file`.

2. **Fill it in.** Any of:

   a. Edit ``biorxiv.sub.txt`` in a text editor, or in your browser with
      ``paperpush web biorxiv.sub.txt`` (see :doc:`../commands/web`).

   b. Ask an AI agent with the ``/paperpush-autofill`` skill:
      ``/paperpush-autofill My manuscript files are in /PATH/TO/MANUSCRIPT/DIRECTORY. Fill out biorxiv.sub.txt``

   c. Use an LLM API (needs the provider's API key):

      .. code-block:: bash

         paperpush autofill --engine api -p /PATH/TO/MANUSCRIPT/DIRECTORY biorxiv.sub.txt

   The AI may not fill every field if some information is not in the
   manuscript directory. See :doc:`../commands/autofill`.

3. **Validate**

   .. code-block:: bash

      paperpush validate biorxiv.sub.txt

   Runs the pre-submission checks on the files and data in the ``.sub``:
   required fields, files, and options, plus the venue's author guidelines
   (section lengths, reference hallucination, figure resolution, link
   validation, anonymization, and more; see :doc:`../validation-checks`). Add
   ``--ai`` (needs an API key), or use the ``/paperpush-validate`` skill, to
   also run the checks that need a reader's judgment. Use the
   ``/paperpush-fix`` skill to fix the issues validate finds: it edits the
   manuscript sources and the ``.sub``, validates again until nothing fixable
   is left, and reports what needs your decision.

   For general manuscript checks, name the files instead of a ``.sub``:

   .. code-block:: bash

      paperpush validate --manuscript main.tex --title-page title.tex --figures fig1.png fig2.png
      paperpush validate --manuscript main.tex --venue biorxiv   # also check the venue's guidelines
      paperpush validate --manuscript main.tex --issn ISSN       # an unlisted journal's publisher house rules

   See :doc:`../commands/validate`.

4. **Share** ``biorxiv.sub.txt`` with co-authors. It can be viewed in a text
   editor, with ``paperpush web biorxiv.sub.txt``, or by uploading it to
   `paperpush.onrender.com <https://paperpush.onrender.com>`_.

5. **Log in** to the venue's submission portal (if available):

   .. code-block:: bash

      paperpush login biorxiv

   Credentials are stored in your OS keyring. See :doc:`../commands/login`.

6. **Submit**: fill out the portal with the information in the ``.sub`` (if
   available):

   .. code-block:: bash

      paperpush submit biorxiv.sub.txt

   PaperPush drives the venue's submission portal in a browser and stops
   **before** the final submit click. Review the form in the portal before
   submitting. Add ``--ai`` (or use the ``/paperpush-portal-debug`` skill) to
   have AI fix problems that come up, and ``--skip-validate`` to ignore
   validation checks. The ``/paperpush-submit`` skill runs login and submit
   together. See :doc:`../commands/submit`.

   ``submit`` is for **initial submissions only**: it opens a new submission,
   so a revision sent this way becomes a duplicate. Upload a revision through
   the venue's own "submit revision" link for the existing manuscript (prepare
   it with ``/paperpush-respond-to-reviewers``).

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
   See :doc:`../commands/venues`.

``paperpush publishers``
   List the publishers whose house rules back their venues' own guidelines.
   Where a venue's guidelines are silent, its publisher's house rules apply,
   reported as warnings. See :doc:`../commands/publishers`.

``paperpush update-venues``
   Update the venue data from the remote venue-data branch. See
   :doc:`../commands/update-venues`.

``paperpush requirements VENUE``
   Print the venue's author guidelines. Rules marked ``*`` come from its
   publisher's house rules. See :doc:`../commands/requirements`.

``paperpush pick-venue``
   List journals that fit your manuscript's topic, from keywords, the
   manuscript file, or ``--title``/``--abstract``: any journal, from the most
   similar works in `OpenAlex <https://openalex.org>`_, with the venues
   PaperPush supports marked. ``--paperpush-supported-only`` ranks only the
   supported venues (offline). It checks thematic fit only, not impact or
   likelihood of acceptance. See :doc:`../commands/pick-venue`.

``paperpush format VENUE.sub.txt``
   Reformat the manuscript (.docx or LaTeX) for the venue: its own Word or
   LaTeX template when one is found, else its layout rules. The original is
   never changed. See :doc:`../commands/format`.

``paperpush convert --manuscript FILE --to docx|tex``
   Convert a manuscript from Markdown to Word or LaTeX, or between Word and
   LaTeX. See :doc:`../commands/convert`.

``paperpush find-reviewers VENUE.sub.txt``
   Suggest reviewers who recently published similar work, leaving out the
   authors, their recent co-authors, and their colleagues; ``--write`` adds
   them to the ``.sub``. See :doc:`../commands/find-reviewers`.

``paperpush options VENUE.FIELD``
   Print the allowed values for a field in the venue's submission form. See
   :doc:`../commands/options`.

``paperpush web VENUE.sub.txt``
   Open a ``.sub`` file in a local copy of the web app. See
   :doc:`../commands/web`.

.. _what-is-a-sub-file:

What is a .sub file?
--------------------

The ``.sub`` file is a YAML-like text file holding all the metadata and file
paths needed to upload a manuscript to a venue's submission portal. In the
snippet below you would fill in the manuscript path, title, and abstract:

.. code-block:: subfile

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

See :doc:`../sub-file` for a full, filled-in example.
