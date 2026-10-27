"""Browser front end for paperpush.

A small FastAPI app (see :mod:`paperpush.web.app`) that serves a single page:
the author picks a venue, uploads the manuscript, fills in the venue's ``.sub``
fields through a form (optionally autofilled from the manuscript), and runs
``validate`` with or without the AI checks -- without installing anything. It
runs the same way hosted (in the Docker image) or locally::

    paperpush-web                 # http://127.0.0.1:8000

Needs the ``web`` extra: ``pip install 'paperpush[web]'``.
"""
