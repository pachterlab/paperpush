``paperpush web``
=================

Open the paperpush web page on this computer. With a ``.sub`` file, the page
opens with that file filled in: every field as a card, coloured by status, with
the side menu, totals, and checks. Each edit you make there is saved back to
the file.

The page runs locally from the installed package, not from the hosted
website. It opens at once (the hosted site can take a while to wake), your
manuscript files never leave your computer, and an API key set in your
environment works for autofill and the AI checks, as it does on the command
line.

.. code-block:: text

   usage: paperpush web [-h] [-v] [-q] [-d DIR] [--port PORT] [--no-browser]
                        [subfile]

Needs the web extra: ``pip install 'paperpush[web]'``.

Synopsis
--------

.. code-block:: bash

   # The page, with no file open yet: pick a venue and start a form.
   paperpush web

   # biorxiv.sub, filled in. Edits are saved to biorxiv.sub.
   paperpush web biorxiv.sub

   # The same, and offer the files in ./manuscript in the file pickers.
   paperpush web biorxiv.sub -d manuscript

The server listens on ``127.0.0.1`` only and runs until you press Ctrl-C.
Paths in the ``.sub`` resolve against the directory you run the command
from, as they do for :doc:`validate` and :doc:`submit`.

``subfile``, ``autofill``, and ``validate`` take ``--web`` to finish in the same
page: ``paperpush validate biorxiv.sub --web`` opens it with the checks already
run.

Options
-------

``subfile``
   A ``.sub`` file to open. It must exist and have an ``@venue:`` line;
   create one with ``paperpush subfile VENUE`` (add ``--web`` to open it
   straight away).

``-d DIR``, ``--directory DIR``
   A manuscript directory whose files the page offers in its file pickers.
   Needs a ``.sub`` file. May be repeated.

``--port PORT``
   The port to listen on. Default: any free port.

``--no-browser``
   Print the page's address instead of opening it in your browser.

Sharing the page
----------------

**Share for editing** (on the hosted site)
   Puts the form online and copies a link. Everyone who opens the link edits
   the same form, with the same word counts, options, and checks, and each
   edit is saved a moment after it is made. If two people change *different*
   fields at once, both changes are kept. If they change the *same* field,
   the one who saves second chooses which value to keep. The form is
   encrypted in the browser with a key that exists only in the link (after
   the ``#``, which browsers never send to a server), so the server stores
   only ciphertext. Anyone with the link can read and edit the form, and a
   lost link cannot be recovered. Uploaded files are not shared: each person
   uploads their own to validate. **Delete for everyone** removes the shared
   form; a form nobody edits for 90 days is deleted. Not available for
   ``--web`` pages, which only run on your computer.

**Copy share link**
   A link holding a frozen copy of the form (inside the link itself, so
   nothing is stored on a server). Whoever opens it gets their own copy to
   edit, saved in their browser; their changes do not come back to you
   unless they send you a new link.

At the bottom of the page, **Save this page as**:

**HTML**
   A single file to send to a PI or collaborator. It opens in any browser
   with no paperpush needed. The side menu, filters, search, light and dark
   switch, and copy buttons still work, but the fields are read-only. A link
   at the top opens an editable copy on the website.

**PDF**
   Opens the browser's print dialog; choose *Save as PDF*. The PDF is laid out
   for paper: light colours, without the editing controls, every check open,
   text boxes at full height, and only the chosen options of long checklists.
