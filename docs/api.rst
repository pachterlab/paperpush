API reference
=============

PaperPush is primarily a command-line tool, but its core is a plain Python
package you can import and drive directly.

The public API is deliberately small — the names exported from :mod:`paperpush`
(its ``__all__``): the two data models, the submission-file toolkit, and the two
workflow operations that have a library-level entry point.

.. code-block:: python

   import paperpush

   venue = paperpush.get_venue("biorxiv")
   sub = paperpush.load("biorxiv.sub")
   issues = paperpush.validate(sub, venue)

.. note::

   **The** ``login`` **and** ``submit`` **steps are exposed only as CLI
   commands** — :doc:`paperpush login <commands/login>` and
   :doc:`paperpush submit <commands/submit>` — not as importable functions.
   They drive a real browser and orchestrate interactive sign-in, so they live
   in the CLI rather than the library API.

   Lower-level helpers (autofill extraction internals, credential storage,
   logging setup) are intentionally **not** part of the public API. They remain
   importable from their submodules if you need them
   (e.g. ``from paperpush import credentials``), but are not covered by
   compatibility guarantees.

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

Apply extracted submission field values to a ``.sub`` file. See
:doc:`commands/autofill` for the CLI equivalent.

.. autofunction:: paperpush.autofill

Validate
--------

Run the pre-submission checks against a loaded ``.sub`` file and its venue. See
:doc:`commands/validate` for the CLI equivalent.

.. autofunction:: paperpush.validate
