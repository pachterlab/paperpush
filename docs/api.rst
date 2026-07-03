API reference
=============

PaperPush is primarily a command-line tool, but its core is a plain Python
package you can import and drive directly. Everything documented here is part of
the public API — the names exported from :mod:`paperpush` (its ``__all__``).

.. code-block:: python

   import paperpush

   venue = paperpush.get_venue("biorxiv")
   sub = paperpush.load("biorxiv.sub")

Package metadata
----------------

.. autodata:: paperpush.__version__
   :no-value:

.. autodata:: paperpush.__url__
   :no-value:

Venues
------

Look up venue definitions and their fields.

.. autofunction:: paperpush.get_venue

.. autofunction:: paperpush.list_venues

.. autoclass:: paperpush.Venue
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: paperpush.Field
   :members:
   :undoc-members:
   :show-inheritance:

Submission files
----------------

Read, write, and render ``.sub`` files.

.. autoclass:: paperpush.SubFile
   :members:
   :undoc-members:
   :show-inheritance:

.. autofunction:: paperpush.load

.. autofunction:: paperpush.parse

.. autofunction:: paperpush.render_template

.. autofunction:: paperpush.write_template

Autofill
--------

Extract submission field values from a manuscript and apply them to a
``.sub`` file. See :doc:`commands/autofill` for the CLI equivalent.

.. autofunction:: paperpush.autofill

.. autofunction:: paperpush.parse_extraction

.. autofunction:: paperpush.effective_role

.. autofunction:: paperpush.field_schema

.. autofunction:: paperpush.extract_via_api

.. autoclass:: paperpush.AutofillResult
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: paperpush.Extraction
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: paperpush.Proposal
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: paperpush.DocumentInput
   :members:
   :undoc-members:
   :show-inheritance:

.. autoexception:: paperpush.AutofillApiError
   :members:
   :show-inheritance:

Credentials
-----------

Store and retrieve venue credentials from the OS keyring. See
:doc:`commands/login` for the CLI equivalent.

.. autofunction:: paperpush.save_credential

.. autofunction:: paperpush.get_credential

.. autofunction:: paperpush.delete_credential

.. autoclass:: paperpush.Credential
   :members:
   :undoc-members:
   :show-inheritance:

Logging
-------

.. autofunction:: paperpush.configure_logging
