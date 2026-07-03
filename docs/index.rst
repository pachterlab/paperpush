PaperPush
=========

**Automated manuscript submission to journals, conferences, and preprint
servers.**

PaperPush prepares manuscripts for submission to preprint servers, journals,
and conferences with just a few commands. Instead of spending hours filling out
submission portals by hand, point PaperPush at a manuscript directory and a
target venue, and it fills out the submission form for you.

The workflow is five short commands:

.. code-block:: bash

   paperpush subfile biorxiv          # 1. create a biorxiv.sub template
   paperpush autofill -d MANUSCRIPT_DIR biorxiv.sub   # 2. fill it from your files
   paperpush validate biorxiv.sub     # 3. run pre-submission checks
   paperpush login biorxiv            # 4. store your credentials
   paperpush submit biorxiv.sub       # 5. drive the submission portal

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
   api
   example-session

Indices
-------

* :ref:`genindex`
* :ref:`search`
