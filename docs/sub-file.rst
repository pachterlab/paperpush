.. _what-is-a-sub-file:

The .sub file
=============

The ``.sub`` file is the YAML-like text file that holds all the metadata and
file paths needed to upload a manuscript to a venue's submission portal. It is
plain text, so you can edit it in any text editor, keep it under version
control, and send it to co-authors.

Each field has a comment above it giving its label, its type, whether it is
**REQUIRED** or *optional*, and help text (with the allowed options for choice
fields). Lines starting with ``#`` are comments and are ignored. Multi-line
answers (abstracts, author lists) follow ``key: |`` on indented lines.

A minimal example
-----------------

In the snippet below you would provide ``/PATH/TO/MANUSCRIPT.pdf``, ``TITLE``,
and ``HERE IS THE ABSTRACT``:

.. code-block:: subfile

   @venue: venue

   # Manuscript file (file, REQUIRED)
   #   Path to the submission.
   manuscript_file: /PATH/TO/MANUSCRIPT.pdf

   # Title (text, REQUIRED)
   #   The full title of the article.
   title: TITLE

   # Abstract (textarea, REQUIRED)
   #   Plain-text abstract, at least 20 characters
   abstract: |
     HERE IS THE ABSTRACT

.. _sub-file-example:

A full example: arXiv
---------------------

This is a filled-in ``.sub`` for arXiv, from the test suite
(`tests/sub_files/arxiv/arxiv.sub.txt
<https://github.com/pachterlab/paperpush/blob/main/tests/sub_files/arxiv/arxiv.sub.txt>`_).
It was created with ``paperpush subfile arxiv`` and then filled in.

.. literalinclude:: ../tests/sub_files/arxiv/arxiv.sub.txt
   :language: subfile

Working with a .sub file
------------------------

- **Create** one with ``paperpush subfile VENUE`` (see :doc:`commands/subfile`),
  or ``paperpush subfile general`` if you have not chosen a venue yet.
- **Fill it in** by hand, in a form with ``paperpush web VENUE.sub.txt`` (see
  :doc:`commands/web`) or on `paperpush.onrender.com
  <https://paperpush.onrender.com>`_, or from your manuscript files with the
  ``/paperpush-autofill`` agent skill or ``paperpush autofill`` (see
  :doc:`commands/autofill`).
- **Check it** with ``paperpush validate VENUE.sub.txt`` (see
  :doc:`validation-checks`).
- **Submit it** with ``paperpush submit VENUE.sub.txt`` for venues with submit
  support (see :doc:`venues`).
- **Share it** by sending the file to co-authors. They can read it in a text
  editor, or open it as a form with ``paperpush web`` or by uploading it to
  `paperpush.onrender.com <https://paperpush.onrender.com>`_.

Syntax highlighting in VS Code
------------------------------

The repository's ``.vscode/settings.json`` treats ``*.sub`` and ``*.sub.txt``
files as YAML and, with the `Highlight
<https://marketplace.visualstudio.com/items?itemName=fabiospampinato.vscode-highlight>`_
extension (``fabiospampinato.vscode-highlight``) installed, colors them as
above: comments grey, **REQUIRED** red, *optional* green, field names teal,
and answers blue. To get the same colors in your own project, copy the
``files.associations`` and ``highlight.*`` settings from that file.
