Installation
============

PaperPush requires **Python 3.10 or newer**.

Install from PyPI
-----------------

.. code-block:: bash

   pip install paperpush

PaperPush drives submission portals with a real browser via `Playwright
<https://playwright.dev/python/>`_. After installing the package, install the
browser binaries once:

.. code-block:: bash

   playwright install

Optional extras
---------------

Some features need extra dependencies, declared as optional extras:

``ai``
   Adds the Anthropic, OpenAI, and Google (``google-genai``) SDKs, used
   wherever paperpush calls an LLM API: ``paperpush autofill --engine api``,
   ``paperpush validate --ai``, and AI recovery during ``paperpush submit``.
   To install only the provider you use, pick ``ai-anthropic``,
   ``ai-openai``, or ``ai-google`` instead.

   .. code-block:: bash

      pip install "paperpush[ai]"                  # all three providers
      pip install "paperpush[ai-openai]"           # just OpenAI

``validate``
   Optional extras for ``validate``:

   - ``pdfminer.six``, which reads PDFs glyph by glyph (colour, render mode,
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
   and friends) for contributing to PaperPush. Includes ``pdfminer.six``,
   ``arxiv_latex_cleaner``, and ``codespell`` so the PDF, cleaner, and
   spelling tests run.

   .. code-block:: bash

      pip install "paperpush[dev]"

Install from source
--------------------

For local development, clone the repository and install in editable mode:

.. code-block:: bash

   git clone https://github.com/pachterlab/paperpush.git
   cd paperpush
   pip install -e ".[dev]"
   playwright install

If you plan to contribute, enable the project git hooks:

.. code-block:: bash

   git config core.hooksPath .githooks

Verify the install
------------------

.. code-block:: bash

   paperpush --version
   paperpush --venues

``--venues`` prints every supported venue with its slug. See
:doc:`venues` for the full list.
