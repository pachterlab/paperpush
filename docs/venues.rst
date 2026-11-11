Supported venues
================

PaperPush identifies each venue by a short **slug**, which you pass to
``subfile``, ``login``, and the like (e.g. ``paperpush subfile biorxiv``).

Every listed venue supports **validate + basic .sub**: ``paperpush validate``
checks the manuscript against the venue's author guidelines, and
``paperpush subfile`` writes a basic submission template. Venues with a check
under **Submit** also have a ``login``/``submit`` runner that fills out
the venue's submission portal, and a complete ``.sub`` template covering every
field the portal asks for.

The table below is generated from ``paperpush/venues.json`` (by
``scripts/gen_readme_venues.py``) and has the same columns as ``paperpush venues``. The always-current list
is on the command line:

.. code-block:: bash

   paperpush venues                          # every venue
   paperpush venues --submit                 # only venues with a submit runner
   paperpush venues --submit -t journal -k biology

See :doc:`commands/venues` for every filter, and :doc:`commands/pick-venue` to
find venues that fit your manuscript's topic.

.. BEGIN SUPPORTED VENUES

*77 supported venues.*

.. list-table::
   :header-rows: 1
   :widths: 18 22 30 10 20 8 8

   * - Slug
     - Venue
     - Keywords
     - Type
     - Link
     - Validate
     - Submit
   * - ``alphaxiv``
     - alphaXiv
     - physics, math, computer science, machine learning, statistics
     - Preprint
     - https://www.alphaxiv.org
     - ✅
     - ✅
   * - ``arxiv``
     - arXiv
     - physics, math, computer science, machine learning, statistics, biology
     - Preprint
     - https://arxiv.org
     - ✅
     - ✅
   * - ``biorxiv``
     - bioRxiv
     - biology, genetics, bioinformatics, neuroscience, immunology
     - Preprint
     - https://www.biorxiv.org
     - ✅
     - ✅
   * - ``medrxiv``
     - medRxiv
     - medicine
     - Preprint
     - https://www.medrxiv.org
     - ✅
     - ✅
   * - ``ajhg``
     - AJHG
     - genetics, medicine
     - Journal
     - https://www.cell.com/ajhg
     - ✅
     -
   * - ``bioinformatics``
     - Bioinformatics
     - bioinformatics, biology, genetics
     - Journal
     - https://academic.oup.com/bioinformatics
     - ✅
     - ✅
   * - ``bmc_bioinformatics``
     - BMC Bioinformatics
     - bioinformatics, biology
     - Journal
     - https://link.springer.com/journal/12859
     - ✅
     - ✅
   * - ``bmc_biology``
     - BMC Biology
     - biology
     - Journal
     - https://bmcbiol.biomedcentral.com
     - ✅
     -
   * - ``bmc_genomics``
     - BMC Genomics
     - genetics, bioinformatics, biology
     - Journal
     - https://bmcgenomics.biomedcentral.com
     - ✅
     -
   * - ``bmc_medicine``
     - BMC Medicine
     - medicine
     - Journal
     - https://bmcmedicine.biomedcentral.com
     - ✅
     -
   * - ``bmj``
     - The BMJ
     - medicine
     - Journal
     - https://www.bmj.com
     - ✅
     -
   * - ``cell``
     - Cell
     - biology, genetics, neuroscience, immunology
     - Journal
     - https://www.cell.com/cell/home
     - ✅
     - ✅
   * - ``cell_genomics``
     - Cell Genomics
     - genetics, biology, bioinformatics
     - Journal
     - https://www.cell.com/cell-genomics/home
     - ✅
     - ✅
   * - ``cell_reports``
     - Cell Reports
     - biology, genetics, neuroscience, immunology
     - Journal
     - https://www.cell.com/cell-reports
     - ✅
     -
   * - ``cell_reports_methods``
     - Cell Reports Methods
     - general
     - Journal
     - https://www.cell.com/cell-reports-methods
     - ✅
     -
   * - ``cell_stem_cell``
     - Cell Stem Cell
     - biology, medicine
     - Journal
     - https://www.cell.com/cell-stem-cell
     - ✅
     -
   * - ``cell_systems``
     - Cell Systems
     - biology, bioinformatics
     - Journal
     - https://www.cell.com/cell-systems/home
     - ✅
     - ✅
   * - ``combinatorica``
     - Combinatorica
     - math, computer science
     - Journal
     - https://link.springer.com/journal/493
     - ✅
     - ✅
   * - ``communications_biology``
     - Communications Biology
     - biology
     - Journal
     - https://www.nature.com/commsbio
     - ✅
     -
   * - ``discrete_mathematics``
     - Discrete Mathematics
     - math
     - Journal
     - https://www.sciencedirect.com/journal/discrete-mathematics
     - ✅
     - ✅
   * - ``elife``
     - eLife
     - biology, neuroscience, medicine
     - Journal
     - https://elifesciences.org
     - ✅
     -
   * - ``european_radiology``
     - European Radiology
     - radiology, medicine
     - Journal
     - https://link.springer.com/journal/330
     - ✅
     -
   * - ``frontiers_bioinformatics``
     - Frontiers in Bioinformatics
     - bioinformatics, biology
     - Journal
     - https://www.frontiersin.org/journals/bioinformatics
     - ✅
     -
   * - ``frontiers_genetics``
     - Frontiers in Genetics
     - genetics, biology
     - Journal
     - https://www.frontiersin.org/journals/genetics
     - ✅
     -
   * - ``frontiers_immunology``
     - Frontiers in Immunology
     - immunology, biology
     - Journal
     - https://www.frontiersin.org/journals/immunology
     - ✅
     -
   * - ``genome_biology``
     - Genome Biology
     - genetics, bioinformatics, biology
     - Journal
     - https://genomebiology.biomedcentral.com
     - ✅
     - ✅
   * - ``genome_medicine``
     - Genome Medicine
     - genetics, medicine, bioinformatics
     - Journal
     - https://genomemedicine.biomedcentral.com
     - ✅
     -
   * - ``genome_research``
     - Genome Research
     - genetics, bioinformatics, biology
     - Journal
     - https://genome.cshlp.org
     - ✅
     -
   * - ``ieee_tmi``
     - IEEE TMI
     - radiology, machine learning, computer vision
     - Journal
     - https://ieeetmi.org/
     - ✅
     -
   * - ``ieee_tnnls``
     - IEEE TNNLS
     - machine learning
     - Journal
     - https://cis.ieee.org/publications/t-neural-networks-and-learning-systems
     - ✅
     -
   * - ``ieee_tpami``
     - IEEE TPAMI
     - computer vision, machine learning
     - Journal
     - https://www.computer.org/csdl/journal/tp
     - ✅
     -
   * - ``iscience``
     - iScience
     - general, biology
     - Journal
     - https://www.cell.com/iscience
     - ✅
     -
   * - ``jama``
     - JAMA
     - medicine
     - Journal
     - https://jamanetwork.com/journals/jama
     - ✅
     -
   * - ``jiim``
     - JIIM
     - radiology, medicine, machine learning, computer science
     - Journal
     - https://link.springer.com/journal/10278
     - ✅
     -
   * - ``jmlr``
     - JMLR
     - machine learning, statistics
     - Journal
     - https://www.jmlr.org
     - ✅
     -
   * - ``jnm``
     - JNM
     - radiology, medicine
     - Journal
     - https://jnm.snmjournals.org
     - ✅
     -
   * - ``lancet``
     - The Lancet
     - medicine
     - Journal
     - https://www.thelancet.com/journals/lancet/home
     - ✅
     -
   * - ``lancet_digital_health``
     - Lancet Digital Health
     - medicine, machine learning
     - Journal
     - https://www.thelancet.com/journals/landig/home
     - ✅
     -
   * - ``medical_image_analysis``
     - Medical Image Analysis
     - radiology, computer vision, machine learning
     - Journal
     - https://www.sciencedirect.com/journal/medical-image-analysis
     - ✅
     -
   * - ``molecular_cell``
     - Molecular Cell
     - biology
     - Journal
     - https://www.cell.com/molecular-cell
     - ✅
     -
   * - ``nar_cancer``
     - NAR Cancer
     - biology, genetics, medicine
     - Journal
     - https://academic.oup.com/narcancer
     - ✅
     -
   * - ``nar_genomics_bioinformatics``
     - NAR Genomics and Bioinformatics
     - genetics, bioinformatics
     - Journal
     - https://academic.oup.com/nargab
     - ✅
     -
   * - ``nature``
     - Nature
     - general, biology, medicine, physics
     - Journal
     - https://www.nature.com
     - ✅
     - ✅
   * - ``nature_biomedical_engineering``
     - Nature Biomedical Engineering
     - medicine, biology
     - Journal
     - https://www.nature.com/natbiomedeng
     - ✅
     - ✅
   * - ``nature_biotech``
     - Nature Biotechnology
     - biology, genetics, bioinformatics, medicine
     - Journal
     - https://www.nature.com/nbt
     - ✅
     - ✅
   * - ``nature_cell_biology``
     - Nature Cell Biology
     - biology
     - Journal
     - https://www.nature.com/ncb
     - ✅
     -
   * - ``nature_communications``
     - Nature Communications
     - general, biology, physics, medicine
     - Journal
     - https://www.nature.com/ncomms
     - ✅
     -
   * - ``nature_computational_science``
     - Nature Computational Science
     - computer science
     - Journal
     - https://www.nature.com/natcomputsci
     - ✅
     -
   * - ``nature_genetics``
     - Nature Genetics
     - genetics, biology, medicine
     - Journal
     - https://www.nature.com/ng
     - ✅
     -
   * - ``nature_machine_intelligence``
     - Nature Machine Intelligence
     - machine learning, robotics
     - Journal
     - https://www.nature.com/natmachintell
     - ✅
     - ✅
   * - ``nature_medicine``
     - Nature Medicine
     - medicine
     - Journal
     - https://www.nature.com/nm
     - ✅
     -
   * - ``nature_methods``
     - Nature Methods
     - biology, bioinformatics, genetics, neuroscience
     - Journal
     - https://www.nature.com/nmeth
     - ✅
     - ✅
   * - ``nature_neuroscience``
     - Nature Neuroscience
     - neuroscience, biology
     - Journal
     - https://www.nature.com/neuro
     - ✅
     -
   * - ``nature_structural_molecular_biology``
     - Nature Structural & Molecular Biology
     - biology
     - Journal
     - https://www.nature.com/nsmb
     - ✅
     -
   * - ``nejm``
     - NEJM
     - medicine
     - Journal
     - https://www.nejm.org
     - ✅
     -
   * - ``neuron``
     - Neuron
     - neuroscience, biology
     - Journal
     - https://www.cell.com/neuron
     - ✅
     -
   * - ``npj_digital_medicine``
     - npj Digital Medicine
     - medicine, machine learning
     - Journal
     - https://www.nature.com/npjdigitalmed
     - ✅
     -
   * - ``nucleic_acids_research``
     - Nucleic Acids Research
     - biology, genetics, bioinformatics
     - Journal
     - https://academic.oup.com/nar
     - ✅
     - ✅
   * - ``patterns``
     - Patterns
     - general
     - Journal
     - https://www.cell.com/patterns
     - ✅
     -
   * - ``plos_biology``
     - PLOS Biology
     - biology
     - Journal
     - https://journals.plos.org/plosbiology/
     - ✅
     -
   * - ``plos_compbio``
     - PLOS Computational Biology
     - bioinformatics, biology, neuroscience
     - Journal
     - https://journal.plos.org/ploscompbiol/
     - ✅
     - ✅
   * - ``plos_genetics``
     - PLOS Genetics
     - genetics, biology
     - Journal
     - https://journals.plos.org/plosgenetics/
     - ✅
     -
   * - ``plos_medicine``
     - PLOS Medicine
     - medicine
     - Journal
     - https://journals.plos.org/plosmedicine/
     - ✅
     -
   * - ``plos_one``
     - PLOS ONE
     - general, biology, medicine
     - Journal
     - https://journals.plos.org/plosone/
     - ✅
     -
   * - ``pnas``
     - PNAS
     - general, biology, physics, math, medicine
     - Journal
     - https://www.pnas.org
     - ✅
     -
   * - ``radiology``
     - Radiology
     - radiology, medicine
     - Journal
     - https://pubs.rsna.org/journal/radiology
     - ✅
     -
   * - ``radiology_ai``
     - Radiology: Artificial Intelligence
     - radiology, machine learning, medicine
     - Journal
     - https://pubs.rsna.org/journal/ai
     - ✅
     -
   * - ``science``
     - Science
     - general, biology, medicine, physics
     - Journal
     - https://www.science.org/journal/science
     - ✅
     - ✅
   * - ``science_advances``
     - Science Advances
     - general, biology, medicine, physics
     - Journal
     - https://www.science.org/journal/sciadv
     - ✅
     - ✅
   * - ``science_immunology``
     - Science Immunology
     - immunology, biology, medicine
     - Journal
     - https://www.science.org/journal/sciimmunol
     - ✅
     - ✅
   * - ``science_robotics``
     - Science Robotics
     - robotics, machine learning
     - Journal
     - https://www.science.org/journal/scirobotics
     - ✅
     - ✅
   * - ``science_signaling``
     - Science Signaling
     - biology
     - Journal
     - https://www.science.org/journal/signaling
     - ✅
     - ✅
   * - ``science_translational_medicine``
     - Science Translational Medicine
     - medicine, biology
     - Journal
     - https://www.science.org/journal/stm
     - ✅
     - ✅
   * - ``scientific_reports``
     - Scientific Reports
     - general
     - Journal
     - https://www.nature.com/srep
     - ✅
     -
   * - ``tmlr``
     - TMLR
     - machine learning
     - Journal
     - https://jmlr.org/tmlr/
     - ✅
     -
   * - ``acl_arr``
     - ACL Rolling Review
     - natural language processing, machine learning
     - Conference
     - https://aclrollingreview.org
     - ✅
     -
   * - ``cvpr_2027``
     - CVPR 2027
     - computer vision, machine learning
     - Conference
     - https://cvpr.thecvf.com/Conferences/2027
     - ✅
     -

.. END SUPPORTED VENUES

Keeping venue data current
--------------------------

Venues are added (for validate) and updated (for validate and submit) daily.
``paperpush update-venues`` pulls the latest venue data immediately (see
:doc:`commands/update-venues`). New ``submit`` runners ship with PaperPush
releases, so upgrade to get them:

.. code-block:: bash

   pip install -U paperpush

Where venue data lives
----------------------

Each venue is described in three JSON databases:

- ``paperpush/venues.json`` — the master list, with each venue's general
  metadata (name, type, homepage, anonymity, subject keywords). See the
  :doc:`venues.json reference <schemas/venues>`.
- ``paperpush/submission_forms.json`` — the fields the portal asks for, which
  become the ``.sub`` file. See the :doc:`submission_forms.json reference
  <schemas/submission_forms>`.
- ``paperpush/manuscript_requirements.json`` — what the venue's author
  guidelines require of the manuscript itself. See the
  :doc:`manuscript_requirements.json reference
  <schemas/manuscript_requirements>`.

The login and submit runner for a venue lives in
``paperpush/venues/<platform>/<venue>.py``. Many venues share a submission
platform (Editorial Manager, ScholarOne Manuscripts, eJournalPress, Snapp,
OpenReview), so their runners are often similar. For more detail per venue —
keywords, submission platform, and the date of the last successful portal
walkthrough — see ``venues.md`` in the repository.

Sample submission files
-----------------------

Every venue with a submit runner has a complete, filled example ``.sub`` file
in the repository under ``tests/sub_files/<venue>/<venue>.sub.txt``. These are
the best reference for a correctly filled submission — see
:doc:`commands/submit`.

.. _adding-a-venue:

Adding a venue
--------------

New venues are welcome.

**With an AI agent** (Claude Code or another agent with the PaperPush skills):

.. code-block:: text

   /paperpush-add-venue Add VENUE (LINK_TO_GUIDELINES) to PaperPush

- For ``validate`` and a basic ``.sub`` only, without ``login``/``submit``:
  ``/paperpush-add-venue-validate Add VENUE (LINK_TO_GUIDELINES) to PaperPush (validate-only)``
- To add ``login``/``submit`` to a venue that already supports ``validate``:
  ``/paperpush-add-venue Add login/submit support for VENUE to PaperPush``

You need an account with the venue so the agent can navigate its portal and
turn it into a script; the agent asks you to log in on the command line once
it has written the login step. Agents can do this autonomously, but it can take
many tokens and may stall; giving the agent the HTML or Playwright snippet from
``paperpush codegen`` (see :doc:`commands/codegen`) for the part where it
stalls helps.

**By hand**, a venue has these components:

1. **Venue listing** — an entry in ``paperpush/venues.json``.
2. **Submission form** (for ``subfile``, ``autofill``, basic ``validate``,
   ``submit``) — an entry in ``paperpush/submission_forms.json`` with the
   fields and constraints, in the order the portal asks for them.
3. **Manuscript requirements** (for ``validate``) — an entry in
   ``paperpush/manuscript_requirements.json`` with the author guidelines'
   rules (formats, length limits, required sections and statements, title-page
   items, figure rules, references, and so on).
4. **Login and submit scripts** (for ``login``, ``submit``) — a ``Venue``
   subclass in ``paperpush/venues/<portal>/<venue>.py``. Start from
   ``paperpush/venues/template/template.py``. It sets ``slug`` and implements
   ``submit()`` (stopping before the final submit); a portal with accounts
   also sets ``logged_in_names`` and implements ``login()``, while a public
   portal sets ``requires_login = False`` and
   ``supports_session_capture = False``. ``paperpush codegen`` records a
   starting point.
5. **Test fixtures** (for ``login``, ``submit``) — a filled sample submission
   in ``tests/sample_subfiles.json``.

Every new venue needs 1–2; 3 is highly recommended, and 4–5 are recommended
where feasible. See the `contributor video tutorial
<https://youtu.be/hn1RNcvHuYw>`_, and ``CONTRIBUTING.md`` and
``DEVELOPMENT.md`` in the repository for the full checklist, including the
scripts and tests to run before opening a pull request.
