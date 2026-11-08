``paperpush publishers``
========================

List the publishers paperpush knows: each one's slug, name, whether paperpush
holds its house rules, and how many listed venues name it as their publisher.

.. code-block:: text

   usage: paperpush publishers [-h] [-v] [-q] [--json]

Synopsis
--------

.. code-block:: bash

   paperpush publishers
   paperpush publishers --json

A publisher's *house rules* are the rules it states for all its journals
(Elsevier's Guide for Authors, Springer Nature's editorial policies, ...). They
are stored in ``manuscript_requirements.json`` as ``$publishers.<slug>.rules``
(and addressed as ``publisher:<slug>``), next to each publisher's name,
author-guidelines URL, and the names Crossref gives it. A venue names its publisher with ``publisher`` in
``venues.json``.

How they are used:

- For a listed venue, its own guidelines win; where they are silent, its
  publisher's house rules apply. ``paperpush requirements VENUE`` marks those
  rules with ``*``.
- For a journal paperpush does not list, ``paperpush validate --publisher``
  (or ``--issn`` / ``--journal``, looked up in Crossref) checks the files
  against the house rules alone. See :ref:`validate-publisher`.

Either way, :doc:`validate` reports what a house rule catches as a warning,
never an error.

Arguments
---------

``--json``
   Print the list as JSON: ``slug``, ``name``, ``house_rules``, ``site_url``,
   ``parent`` (for an imprint), and ``venues``.

Plus the common ``-v/--verbose`` and ``-q/--quiet`` logging flags.

See also
--------

- :doc:`requirements` — a venue's rules, or ``publisher:<slug>`` for a
  publisher's house rules.
- :doc:`validate` — ``--publisher``, ``--issn``, ``--journal``.
