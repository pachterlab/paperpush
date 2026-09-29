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

``autofill``
   Adds the Anthropic, OpenAI, and Google (``google-genai``) SDKs, used by
   ``paperpush autofill --engine api`` to extract submission fields with an
   LLM. To install only the provider you use, pick ``autofill-anthropic``,
   ``autofill-openai``, or ``autofill-google`` instead.

   .. code-block:: bash

      pip install "paperpush[autofill]"            # all three providers
      pip install "paperpush[autofill-openai]"     # just OpenAI

``validate``
   Optional extras for ``validate``:

   - ``pdfminer.six``, which reads PDFs glyph by glyph (colour, render mode,
     position) for two checks: hidden text / prompt injection and the
     modified-template check. Without it those checks skip PDFs and
     ``validate`` prints a warning saying so. Recommended if you submit to
     conferences.
   - ``arxiv_latex_cleaner``, which ``validate --arxiv-latex-cleaner`` runs on
     your LaTeX source before checking it.

   .. code-block:: bash

      pip install "paperpush[validate]"

``dev``
   Test and lint tooling (``pytest``, ``bandit``, ``black``, ``jsonschema``,
   and friends) for contributing to PaperPush. Includes ``pdfminer.six`` and
   ``arxiv_latex_cleaner`` so the PDF and cleaner tests run.

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
