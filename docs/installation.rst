Installation
============

.. tip::

   Don't want to install anything? The PaperPush website,
   `paperpush.onrender.com <https://paperpush.onrender.com>`_, lets you
   prepare a submission in your browser, validate it, and share the editable
   form with co-authors.

PaperPush requires **Python 3.10 or newer**.

Install from PyPI
-----------------

.. code-block:: bash

   pip install "paperpush[all]"   # recommended
   pip install paperpush          # basic CLI only

``paperpush[all]`` adds AI API support, the MCP server, the local web app
(``paperpush web``), and the extra manuscript validation checks. The individual
extras are described under `Optional extras`_.

``paperpush submit`` drives submission portals with a real browser via
`Playwright <https://playwright.dev/python/>`_. To use it, install the Chromium
browser once:

.. code-block:: bash

   playwright install chromium

.. _install-the-ai-agent-skills:

Install the AI agent skills
---------------------------

To use PaperPush through an AI agent, install the PaperPush skills so the
agent knows how to autofill, validate, and submit.

**Claude Code** (as two separate prompts):

.. code-block:: text

   /plugin marketplace add pachterlab/paperpush

.. code-block:: text

   /plugin install paperpush@paperpush

**Codex:**

.. code-block:: bash

   codex plugin marketplace add pachterlab/paperpush
   codex plugin add paperpush@paperpush

**From a pip install** (any agent):

.. code-block:: bash

   paperpush install-skills                 # Claude Code, every project (~/.claude/skills)
   paperpush install-skills --agent codex   # Codex (~/.codex/skills)
   paperpush install-skills --dest DIR      # any other agent's skills directory

See :doc:`commands/install-skills` for the skill list and options.

.. _mcp-server:

MCP server
----------

To drive PaperPush from any MCP client (Claude Desktop, an IDE extension), run
it as an MCP server. Install the ``mcp`` extra (included in ``ai`` and
``all``):

.. code-block:: bash

   pip install "paperpush[mcp]"

and add it to your client's configuration:

.. code-block:: json

   {
     "mcpServers": {
       "paperpush": {
         "command": "paperpush-mcp"
       }
     }
   }

The server exposes the whole pipeline as tools. ``AGENTS.md`` in the repository
(or ``paperpush --agent-guide`` from a pip install) lists the tools and the
contract agents should follow.

Optional extras
---------------

``all``
   ``ai``, ``web``, and ``validate`` together. The recommended install.

``ai``
   The Anthropic, OpenAI, and Google (``google-genai``) SDKs plus the MCP
   server, used wherever PaperPush calls an LLM API:
   ``paperpush autofill --engine api``, ``paperpush validate --ai``,
   ``paperpush pick-venue --ai``, and AI recovery during
   ``paperpush submit --ai``. To install only the provider you use, pick
   ``ai-anthropic``, ``ai-openai``, or ``ai-google`` instead.

   .. code-block:: bash

      pip install "paperpush[ai]"                  # all three providers + MCP
      pip install "paperpush[ai-openai]"           # just OpenAI

``mcp``
   The MCP server (``paperpush-mcp``) on its own.

``web``
   The local web app, ``paperpush web`` (FastAPI and uvicorn). See
   :doc:`commands/web`.

``validate``
   Optional extras for ``validate``:

   - ``pdfminer.six``, which reads PDFs glyph by glyph (color, render mode,
     position) for two checks: hidden text / prompt injection and the
     modified-template check. Without it those checks skip PDFs and
     ``validate`` prints a warning saying so. Recommended if you submit to
     conferences.
   - ``arxiv_latex_cleaner``, which ``validate --arxiv-latex-cleaner`` runs on
     your LaTeX source before checking it.
   - ``codespell``, whose list of common misspellings the prose check uses.
     Without it the spelling check is reported as not checked.
   - ``tqdm``, for the progress bar ``validate`` shows in a terminal while it
     runs. Without it ``validate`` runs silently until the report prints.

   .. code-block:: bash

      pip install "paperpush[validate]"

``dev``
   Test and lint tooling (``pytest``, ``bandit``, ``black``, ``jsonschema``,
   ``sphinx``, and friends) for contributing to PaperPush, plus the ``mcp``,
   ``web``, and ``validate`` extras so the full test suite runs.

   .. code-block:: bash

      pip install "paperpush[dev]"

Install from source
-------------------

For local development, clone the repository and install in editable mode:

.. code-block:: bash

   git clone https://github.com/pachterlab/paperpush.git
   cd paperpush
   pip install -e ".[dev]"
   playwright install chromium

If you plan to contribute, enable the project git hooks:

.. code-block:: bash

   git config core.hooksPath .githooks

See ``CONTRIBUTING.md`` and ``DEVELOPMENT.md`` in the repository, and
:ref:`adding a venue <adding-a-venue>`.

Verify the install
------------------

.. code-block:: bash

   paperpush --version
   paperpush venues

``paperpush venues`` prints every supported venue with its slug. See
:doc:`venues` for the full list.
