``paperpush pick-venue``
========================

Find venues for a manuscript, from its keywords, title, and abstract.

.. code-block:: text

   usage: paperpush pick-venue [-h] [-v] [-q] [--manuscript PATH] [--title TEXT]
                               [--abstract TEXT] [-k KW[,KW...]]
                               [--venue-name NAME] [-t TYPE] [--portal PORTAL]
                               [-n N] [--all] [--ai]
                               [--provider {anthropic,openai,google}]
                               [--model MODEL] [--json]
                               [KEYWORD ...]

``pick-venue`` ranks the venues PaperPush supports and ends with the
``paperpush subfile`` command for the top one. It ranks on thematic fit only,
not on impact or the chance of acceptance, and it only knows the venues listed
by ``paperpush venues``.

**Subjects.** Every venue carries subject keywords from a fixed list: general,
biology, genetics, bioinformatics, immunology, neuroscience, medicine,
radiology, machine learning, computer vision, natural language processing,
robotics, math, statistics, physics, and computer science. This is the list
``paperpush venues --keywords`` filters on. ``pick-venue`` works out which of
these subjects a manuscript covers and ranks the venues by them. A venue
scores higher when it covers more of the manuscript's subjects, and higher
again when it is focused on them, so *Science Immunology* ranks above
*bioRxiv* for an immunology paper. A venue tagged ``general`` covers any
subject, at half weight.

**The manuscript's keywords, title, and abstract** are what the subjects are
worked out from:

- Keywords are the positional arguments, written as in a paper's keyword
  line: words form a phrase, and commas separate keywords
  (``pick-venue single cell RNA-seq, T cell exhaustion`` gives two keywords).
  ``-k`` adds more. A keyword that names a subject (``immunology``) also
  matches venues directly.
- ``--title`` and ``--abstract`` give the title and abstract.
- ``--manuscript`` reads all three from a file: ``.tex``, ``.zip``, ``.docx``,
  or ``.pdf``, read the same way ``paperpush validate`` reads them, or a
  ``.txt`` or ``.md`` file holding a title, abstract, and ``Keywords:`` line.
  A single positional argument naming an existing file is read the same way.
  ``--title`` and ``--abstract`` replace what was read from the file, and
  keywords are added to the file's. If no abstract can be found, the start of
  the manuscript text is used instead.

A built-in word list for each subject turns these into subjects (for example,
``hippocampus``, ``synaptic``, and ``fMRI`` point to neuroscience). Each part is
weighted separately: words in the keywords and title count three times as much
as words in the abstract. Longer phrases are matched first, so "T cell" counts
toward immunology rather than biology's "cell". This runs offline and gives
the same answer every time, but it only reads words. The output names the
subjects it inferred and the words behind each one, so you can check them.

``--ai`` asks an LLM instead: it chooses the subjects and ranks the venues,
with a one-sentence reason for each. It uses the same providers, API keys, and
default models as ``paperpush autofill --engine api``. Agents using the MCP
server call the ``pick_venue`` tool (same inputs), read the manuscript
themselves, and pass the subjects they chose as keywords.

**Venue names.** ``--venue-name`` (or ``--journal-name``, ``--conference-name``)
searches venues by slug, name, and full name instead: ``--venue-name "nature
methods"`` finds Nature Methods, ``--venue-name neuro`` finds Neuron and Nature
Neuroscience. An exact name or slug ranks highest; function words such as
"the" and "of" are ignored. Combined with keywords, the scores add up.

Synopsis
--------

.. code-block:: bash

   paperpush pick-venue hippocampus, place cells          # keywords
   paperpush pick-venue immunology, genetics              # subjects, directly
   paperpush pick-venue manuscript.pdf                    # read the manuscript
   paperpush pick-venue --manuscript manuscript.pdf neuroscience   # plus a keyword
   paperpush pick-venue --title "$TITLE" --abstract "$(cat abstract.txt)" memory
   paperpush pick-venue manuscript.pdf --ai               # an LLM's ranking, with reasons
   paperpush pick-venue manuscript.tex --json             # for scripts and agents
   paperpush pick-venue --venue-name "nature methods"     # by name
   paperpush pick-venue -t preprint                       # every preprint server

Options
-------

``KEYWORD``
   The manuscript's keywords: words form a phrase, commas separate keywords.
   A single existing file is read as ``--manuscript`` instead.

``--manuscript PATH``
   A manuscript to read the title, abstract, and keywords from, or a ``.txt``
   or ``.md`` file holding them.

``--title TEXT``
   The manuscript's title. Replaces the one read from ``--manuscript``.

``--abstract TEXT``
   The manuscript's abstract. Replaces the one read from ``--manuscript``.

``-k KW[,KW...]``, ``--keywords KW[,KW...]``
   More keywords, comma-separated; may be repeated. Added to the positional
   keywords and to those read from ``--manuscript``.

``--venue-name NAME``, ``--journal-name NAME``, ``--conference-name NAME``
   Search venues by slug, name, and full name.

``-t TYPE``, ``--type TYPE``
   Only venues of this kind: ``preprint``, ``journal``, or ``conference``.
   May be repeated.

``--portal PORTAL``
   Only venues that submit through this portal (e.g. ``scholarone``). May be
   repeated.

``-n N``, ``--limit N``
   Show at most N venues. Default: 5. ``--all`` shows every match.

``--ai``
   Ask an LLM to choose the subjects and rank the venues, with a reason for
   each. Needs ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, or
   ``GEMINI_API_KEY``/``GOOGLE_API_KEY``, and that provider's SDK
   (``pip install 'paperpush[ai-anthropic]'``, or ``ai-openai`` /
   ``ai-google``). ``--provider`` and ``--model`` choose the provider and
   model.

``--json``
   Print the result as JSON. It includes ``matches`` (each with ``slug``,
   ``name``, ``venue_type``, ``portal``, ``keywords``, ``score``, and
   ``reasons``), ``total_matches``, the ``subjects`` used and the
   ``inferred_subjects`` behind them, what was read from the ``manuscript``,
   the ``venue_name`` searched, and the ``ai`` ranking (``null`` without
   ``--ai``).

Exit status
-----------

0 when at least one venue is listed. 1 when nothing matched, when no subject
could be inferred (add a subject as a keyword, or use ``--ai``), or when the
``--ai`` call failed. 2 for bad arguments.
