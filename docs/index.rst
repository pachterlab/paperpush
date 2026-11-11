PaperPush
=========

.. image:: _static/logo.svg
   :width: 140
   :align: center
   :alt: PaperPush logo

**A tool and AI companion for scientific publishing.**

Prepare manuscripts for submission to preprint servers, journals, and
conferences with just a few commands.

PaperPush provides the following services:

- A standardized system to share all metadata submitted to a venue (the
  ``.sub`` file; :ref:`see an example here <sub-file-example>`).
- Extensive venue-specific manuscript guideline checking, including section
  lengths, reference hallucination, figure resolution, link validation, and
  anonymization. The checks in a report depend on the venue and files
  (:doc:`see the full list <validation-checks>`).
- A central database of journal and conference
  :doc:`manuscript requirements <schemas/manuscript_requirements>`
  (``paperpush/manuscript_requirements.json``) and
  :doc:`submission data <schemas/submission_forms>`
  (``paperpush/submission_forms.json``).
- Automated and fast submission portal filling with deterministic scripts.
- AI skills with an MCP server, a `website <https://paperpush.onrender.com>`_,
  and a :doc:`command line interface <commands/index>`.
- Free, open source, actively maintained, and never stores your data.

Quickstart
----------

Choose your favorite mode: AI agent skills, website, or command line. All
modes support venue-specific metadata collection, venue-tailored checking, and
submission portal filling for :doc:`supported venues <venues>`. AI agents
support additional tasks (choosing a venue, fixing validation issues,
transferring between venues, and responding to reviewers).

:doc:`quickstart/ai`
   Install in Claude Code (:ref:`other agents <install-the-ai-agent-skills>`):

   .. code-block:: text

      /plugin marketplace add pachterlab/paperpush
      /plugin install paperpush@paperpush
      /paperpush-prepare-submission Prepare my manuscript in /PATH/TO/MANUSCRIPT/DIRECTORY for submission to VENUE with PaperPush.

:doc:`quickstart/web`
   No install needed. Visit
   `paperpush.onrender.com <https://paperpush.onrender.com>`_, select the
   venue (or "General"), upload your files, validate, share the editable form
   with co-authors, and fill out the portal automatically when available.

:doc:`quickstart/cli`
   .. code-block:: bash

      pip install "paperpush[all]" && playwright install chromium
      paperpush subfile VENUE               # create VENUE.sub.txt
      #                                       fill out VENUE.sub.txt
      paperpush validate VENUE.sub.txt      # check files and metadata against the author guidelines
      paperpush login VENUE                 # log in to the portal (if available)
      paperpush submit VENUE.sub.txt        # fill out the submission portal (if available)

.. note::

   PaperPush never presses the final submit button. It brings you to one of
   the last steps of the venue's portal; review the form there before
   submitting. ``submit`` is for **initial submissions only**: upload a
   revision through the venue's own "submit revision" link.

.. toctree::
   :maxdepth: 2
   :caption: Getting started

   installation
   quickstart

.. toctree::
   :maxdepth: 2
   :caption: Commands

   commands/index

.. toctree::
   :maxdepth: 1
   :caption: Reference

   venues
   sub-file
   validation-checks
   submission-portal
   schemas/venues
   schemas/submission_forms
   schemas/manuscript_requirements
   api
   example-session

Indices
-------

* :ref:`genindex`
* :ref:`search`
