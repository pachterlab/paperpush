``paperpush pick-venue``
========================

Find journals for a manuscript, from its keywords, title, and abstract.

.. code-block:: text

   usage: paperpush pick-venue [-h] [-v] [-q] [--manuscript PATH] [--title TEXT]
                               [--abstract TEXT] [-k KW[,KW...]]
                               [--venue-name NAME] [-t TYPE] [--portal PORTAL]
                               [-n N] [--all] [--paperpush-supported-only] [--ai]
                               [--provider {anthropic,openai,google}]
                               [--model MODEL] [--json]
                               [KEYWORD ...]

``pick-venue`` ranks venues on thematic fit only, not on impact or the chance
of acceptance. It has two rankings.

Any journal (the default)
-------------------------

Given a manuscript (or its title, abstract, or keywords), ``pick-venue`` looks
up the works most similar to it in `OpenAlex <https://openalex.org>`_ and
groups them by the journal, conference, or preprint server that published them:

- The search ORs together the keywords, the title's words, and the abstract's
  most frequent words. Each work found is then scored by how much of its title
  and abstract it shares with the manuscript (TF-IDF similarity), and works
  that share little are dropped, as is the manuscript itself if OpenAlex
  already has it.
- A journal scores the sum of its works' similarity, with works from the last
  three years counting fully and older ones less; only works from the last
  eight years count. The best journal scores 100.
- Aggregators such as PubMed Central are left out; preprint servers (bioRxiv,
  arXiv, ...) are kept. ``-t journal``, ``-t conference``, or ``-t preprint``
  narrows the kinds kept.

Each journal is listed with its publisher, how many similar works it
published (and the closest one), its h-index, whether it is open access (and
in DOAJ), and its article processing charge, as OpenAlex records them. A
journal PaperPush supports is marked with its slug and what PaperPush can do
for it (``validate``, ``submit``), and the output ends with the
``paperpush subfile`` command for the best-ranked supported one. For a
journal PaperPush does not support, ``paperpush validate --manuscript FILE``
still runs the general checks, and ``/paperpush-add-venue-validate`` can add
its guidelines.

This needs network access. OpenAlex's free tier is shared by everyone on the
same IP address; set ``OPENALEX_API_KEY`` to use your own key. No e-mail
address or other personal information is sent. If OpenAlex cannot be reached
(or finds nothing), a note says so on stderr and ``pick-venue`` ranks the
supported venues instead, as below.

Venues PaperPush supports (``--paperpush-supported-only``)
----------------------------------------------------------

With ``--paperpush-supported-only`` -- and always for ``--venue-name``,
``--portal``, ``--ai``, and filters with no manuscript input -- ``pick-venue``
ranks only the venues listed by ``paperpush venues``, offline.

**Subjects.** Every supported venue carries subject keywords from a fixed list: general,
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

   paperpush pick-venue manuscript.pdf                    # any journal, from OpenAlex
   paperpush pick-venue manuscript.pdf -t journal -n 10   # journals only, ten of them
   paperpush pick-venue manuscript.pdf --paperpush-supported-only   # supported venues only, offline
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
   May be repeated. For the any-journal ranking, ``preprint`` keeps preprint
   servers.

``--portal PORTAL``
   Only venues that submit through this portal (e.g. ``scholarone``). May be
   repeated.

``-n N``, ``--limit N``
   Show at most N venues. Default: 5. ``--all`` shows every match.

``--paperpush-supported-only``
   Rank only the venues PaperPush supports, offline, instead of searching
   OpenAlex for any journal.

``--ai``
   Ask an LLM to choose the subjects and rank the supported venues, with a reason for
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
   ``--ai``). ``openalex`` holds the any-journal ranking: the ``search``
   used, ``similar_works``, ``since_year``, ``total_journals``, and
   ``journals`` (each with ``name``, ``openalex_id``, ``source_type``,
   ``score``, ``similar_works``, ``examples``, ``publisher``, ``issn_l``,
   ``homepage``, ``is_oa``, ``is_in_doaj``, ``apc_usd``, ``h_index``,
   ``paperpush_slug``, and ``paperpush_support``). It is ``{"error": ...}``
   when OpenAlex could not be reached, and ``null`` when OpenAlex was not
   asked. The MCP tool ``pick_venue`` returns the same, with
   ``paperpush_supported_only`` for the flag.

Exit status
-----------

0 when at least one venue or journal is listed. 1 when nothing matched, when no subject
could be inferred (add a subject as a keyword, or use ``--ai``), or when the
``--ai`` call failed. 2 for bad arguments.
