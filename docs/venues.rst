Supported venues
================

PaperPush identifies each venue by a short **slug**, which you pass to
``subfile``, ``login``, and the like (e.g. ``paperpush subfile biorxiv``). The
authoritative, always-current list is available on the command line:

.. code-block:: bash

   paperpush --venues

The venues supported at the time of writing are below; those marked *validate
only* have author-guideline checks for ``paperpush validate`` but no ``submit``
runner yet. For a complete description of each venue's fields and portal, see
``venues.md`` in the repository. The keys a venue entry may carry are
documented in the :doc:`venues.json reference <schemas/venues>` (the master
list), its submission form in the :doc:`submission_forms.json reference
<schemas/submission_forms>`, and what each venue's author guidelines require of
the manuscript in the :doc:`manuscript_requirements.json reference
<schemas/manuscript_requirements>`.

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
   * - `AJHG <https://www.cell.com/ajhg>`_ (validate only)
     - ``ajhg``
   * - `Bioinformatics <https://academic.oup.com/bioinformatics>`_
     - ``bioinformatics``
   * - `BMC Bioinformatics <https://link.springer.com/journal/12859>`_
     - ``bmc_bioinformatics``
   * - `Cell <https://www.cell.com/cell/home>`_
     - ``cell``
   * - `Cell Genomics <https://www.cell.com/cell-genomics/home>`_
     - ``cell_genomics``
   * - `Cell Reports <https://www.cell.com/cell-reports>`_ (validate only)
     - ``cell_reports``
   * - `Cell Systems <https://www.cell.com/cell-systems/home>`_
     - ``cell_systems``
   * - `Combinatorica <https://link.springer.com/journal/493>`_
     - ``combinatorica``
   * - `Discrete Mathematics <https://www.sciencedirect.com/journal/discrete-mathematics>`_
     - ``discrete_mathematics``
   * - `eLife <https://elifesciences.org>`_ (validate only)
     - ``elife``
   * - `European Radiology <https://link.springer.com/journal/330>`_ (validate only)
     - ``european_radiology``
   * - `Genome Biology <https://genomebiology.biomedcentral.com>`_
     - ``genome_biology``
   * - `Genome Medicine <https://genomemedicine.biomedcentral.com>`_ (validate only)
     - ``genome_medicine``
   * - `Genome Research <https://genome.cshlp.org>`_ (validate only)
     - ``genome_research``
   * - `IEEE TMI <https://ieeetmi.org/>`_ (validate only)
     - ``ieee_tmi``
   * - `IEEE TNNLS <https://cis.ieee.org/publications/t-neural-networks-and-learning-systems>`_ (validate only)
     - ``ieee_tnnls``
   * - `IEEE TPAMI <https://www.computer.org/csdl/journal/tp>`_ (validate only)
     - ``ieee_tpami``
   * - `JAMA <https://jamanetwork.com/journals/jama>`_ (validate only)
     - ``jama``
   * - `JMLR <https://www.jmlr.org>`_ (validate only)
     - ``jmlr``
   * - `JNM <https://jnm.snmjournals.org>`_ (validate only)
     - ``jnm``
   * - `Lancet Digital Health <https://www.thelancet.com/journals/landig/home>`_ (validate only)
     - ``lancet_digital_health``
   * - `Medical Image Analysis <https://www.sciencedirect.com/journal/medical-image-analysis>`_ (validate only)
     - ``medical_image_analysis``
   * - `Nature <https://www.nature.com>`_
     - ``nature``
   * - `Nature Biomedical Engineering <https://www.nature.com/natbiomedeng>`_ (validate only)
     - ``nature_biomedical_engineering``
   * - `Nature Biotechnology <https://www.nature.com/nbt>`_
     - ``nature_biotech``
   * - `Nature Communications <https://www.nature.com/ncomms>`_ (validate only)
     - ``nature_communications``
   * - `Nature Genetics <https://www.nature.com/ng>`_ (validate only)
     - ``nature_genetics``
   * - `Nature Machine Intelligence <https://www.nature.com/natmachintell>`_ (validate only)
     - ``nature_machine_intelligence``
   * - `Nature Medicine <https://www.nature.com/nm>`_ (validate only)
     - ``nature_medicine``
   * - `Nature Methods <https://www.nature.com/nmeth>`_
     - ``nature_methods``
   * - `NEJM <https://www.nejm.org>`_ (validate only)
     - ``nejm``
   * - `Neuron <https://www.cell.com/neuron>`_ (validate only)
     - ``neuron``
   * - `npj Digital Medicine <https://www.nature.com/npjdigitalmed>`_ (validate only)
     - ``npj_digital_medicine``
   * - `Nucleic Acids Research <https://academic.oup.com/nar>`_
     - ``nucleic_acids_research``
   * - `PLOS Computational Biology <https://journal.plos.org/ploscompbiol/>`_
     - ``plos_compbio``
   * - `PNAS <https://www.pnas.org>`_ (validate only)
     - ``pnas``
   * - `Radiology <https://pubs.rsna.org/journal/radiology>`_ (validate only)
     - ``radiology``
   * - `Radiology: Artificial Intelligence <https://pubs.rsna.org/journal/ai>`_ (validate only)
     - ``radiology_ai``
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
   * - `The BMJ <https://www.bmj.com>`_ (validate only)
     - ``bmj``
   * - `The Lancet <https://www.thelancet.com/journals/lancet/home>`_ (validate only)
     - ``lancet``
   * - `TMLR <https://jmlr.org/tmlr/>`_ (validate only)
     - ``tmlr``

Conferences
-----------

.. list-table::
   :header-rows: 1
   :widths: 30 25

   * - Venue
     - Slug
   * - `AAAI 2027 <https://aaai.org/conference/aaai/aaai-27/>`_
     - ``aaai_2027``
   * - `ACL Rolling Review <https://aclrollingreview.org>`_ (validate only)
     - ``acl_arr``
   * - `AISTATS 2027 <https://virtual.aistats.org/Conferences/2027>`_ (validate only)
     - ``aistats_2027``
   * - `CVPR 2027 <https://cvpr.thecvf.com/Conferences/2027>`_ (validate only)
     - ``cvpr_2027``
   * - `ICLR 2027 <https://iclr.cc/Conferences/2027>`_
     - ``iclr_2027``

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
