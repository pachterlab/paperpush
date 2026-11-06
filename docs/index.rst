PaperPush
=========

**Automated manuscript submission to journals, conferences, and preprint
servers.**

PaperPush prepares manuscripts for submission to preprint servers, journals,
and conferences with just a few commands. Instead of spending hours checking
manuscript files against author guidelines and filling out submission portals
by hand, give PaperPush a manuscript directory and a target venue: it validates
the documents and fills out the submission portal for you.

PaperPush provides:

- a standardized file (the ``.sub`` file) holding everything submitted to a
  journal or conference, which you can share with co-authors;
- extensive venue-specific guideline checking, including section lengths,
  reference hallucination, figure resolution, link validation, and
  anonymization (see :doc:`validation-checks`);
- a central database of journal and conference manuscript requirements;
- fast, automated submission-portal filling with deterministic scripts;
- all of it free, open source, actively maintained, and never storing your
  data.

Get started
-----------

Pick whichever path suits you; :doc:`quickstart` walks through each one.

**With an AI agent** (Claude Code shown; see :doc:`installation` for Codex and
other agents):

.. code-block:: text

   /plugin marketplace add pachterlab/paperpush
   /plugin install paperpush@paperpush
   /paperpush-prepare-submission Prepare my manuscript in /PATH/TO/MANUSCRIPT/DIRECTORY for submission to VENUE with PaperPush.

**On the website**: no install needed. Visit
`paperpush.onrender.com <https://paperpush.onrender.com>`_, pick a venue,
upload your files, validate, and share the editable form with co-authors.

**On the command line**:

.. code-block:: bash

   paperpush subfile VENUE               # 1. create a VENUE.sub.txt template
   #                                       2. fill out VENUE.sub.txt (by hand, paperpush web, or paperpush autofill)
   paperpush validate VENUE.sub.txt      # 3. check files and metadata against the author guidelines
   paperpush login VENUE                 # 4. log in to the portal (venues with submit support)
   paperpush submit VENUE.sub.txt        # 5. fill out the submission portal

.. note::

   ``paperpush submit`` fills out the venue's portal but **does not click the
   final submit button**. Always review the form in the browser before
   submitting.

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
   validation-checks
   schemas/venues
   schemas/submission_forms
   schemas/manuscript_requirements
   api
   example-session

Indices
-------

* :ref:`genindex`
* :ref:`search`
