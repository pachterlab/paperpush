Supported venues
================

PaperPush identifies each venue by a short **slug**, which you pass to
``subfile``, ``login``, and the like (e.g. ``paperpush subfile biorxiv``). The
authoritative, always-current list is available on the command line:

.. code-block:: bash

   paperpush --venues

The venues supported at the time of writing are below. For a complete
description of each venue's fields and portal, see ``venues.md`` in the
repository.

Preprint servers
----------------

.. list-table::
   :header-rows: 1
   :widths: 30 25

   * - Venue
     - Slug
   * - `arXiv <https://arxiv.org>`_
     - ``arxiv``
   * - `bioRxiv <https://www.biorxiv.org>`_
     - ``biorxiv``
   * - `medRxiv <https://www.medrxiv.org>`_
     - ``medrxiv``

Journals
--------

.. list-table::
   :header-rows: 1
   :widths: 30 25

   * - Venue
     - Slug
   * - `Bioinformatics <https://academic.oup.com/bioinformatics>`_
     - ``bioinformatics``
   * - `BMC Bioinformatics <https://link.springer.com/journal/12859>`_
     - ``bmc_bioinformatics``
   * - `Cell <https://www.cell.com/cell/home>`_
     - ``cell``
   * - `Cell Genomics <https://www.cell.com/cell-genomics/home>`_
     - ``cell_genomics``
   * - `Cell Systems <https://www.cell.com/cell-systems/home>`_
     - ``cell_systems``
   * - `Discrete Mathematics <https://www.sciencedirect.com/journal/discrete-mathematics>`_
     - ``discrete_mathematics``
   * - `Genome Biology <https://genomebiology.biomedcentral.com>`_
     - ``genome_biology``
   * - `Nature <https://www.nature.com>`_
     - ``nature``
   * - `Nature Biotechnology <https://www.nature.com/nbt>`_
     - ``nature_biotech``
   * - `Nature Methods <https://www.nature.com/nmeth>`_
     - ``nature_methods``
   * - `Nucleic Acids Research <https://academic.oup.com/nar>`_
     - ``nucleic_acids_research``
   * - `PLOS Computational Biology <https://journals.plos.org/ploscompbiol/>`_
     - ``plos_compbio``
   * - `Science <https://www.science.org/journal/science>`_
     - ``science``
   * - `Science Advances <https://www.science.org/journal/sciadv>`_
     - ``science_advances``
   * - `Science Immunology <https://www.science.org/journal/sciimmunol>`_
     - ``science_immunology``
   * - `Science Robotics <https://www.science.org/journal/scirobotics>`_
     - ``science_robotics``
   * - `Science Signaling <https://www.science.org/journal/signaling>`_
     - ``science_signaling``
   * - `Science Translational Medicine <https://www.science.org/journal/stm>`_
     - ``science_translational_medicine``

Conferences
-----------

.. list-table::
   :header-rows: 1
   :widths: 30 25

   * - Venue
     - Slug
   * - `AAAI 2027 <https://aaai.org/conference/aaai/aaai-27/>`_
     - ``aaai_2027``

Sample submission files
-----------------------

Every supported venue has a complete, filled example ``.sub`` file in the
repository under ``tests/sub_files/<venue>/<venue>.sub``. These are the best
reference for a correctly filled submission — see :doc:`commands/submit`.

Adding a venue
--------------

New venues are welcome. See ``CONTRIBUTING.md`` and ``DEVELOPMENT.md`` in the
repository for the step-by-step process of defining a venue's fields,
submission script, and tests.
