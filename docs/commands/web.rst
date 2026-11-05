``paperpush web``
=================

Open the paperpush web page on this computer. With a ``.sub`` file, the page
opens with that file filled in: every field as a card, coloured by status, with
the side menu, totals, and checks. Each edit you make there is saved back to
the file.

The checks use the same colours as ``paperpush validate``: green for passed,
red for failed, amber for warnings, purple for needs AI, and grey for checks
that could not run (with the reason) or that paperpush has not implemented.
*Colorblind-friendly colors* in the settings menu switches to the palette of
``paperpush validate --colorblind`` (blue, vermillion, dark yellow, reddish
purple, grey).

The page runs locally from the installed package, not from the hosted
website. It opens at once (the hosted site can take a while to wake), your
manuscript files never leave your computer, and an API key set in your
environment works for autofill and the AI checks, as it does on the command
line.

.. code-block:: text

   usage: paperpush web [-h] [-v] [-q] [-p PATH] [--port PORT] [--no-browser]
                        [subfile]

Needs the web extra: ``pip install 'paperpush[web]'``.

Synopsis
--------

.. code-block:: bash

   # The page, with no file open yet: pick a venue and start a form.
   paperpush web

   # biorxiv.sub.txt, filled in. Edits are saved to biorxiv.sub.txt.
   paperpush web biorxiv.sub.txt

   # The same, and offer the files in ./manuscript in the file pickers.
   paperpush web biorxiv.sub.txt -p manuscript

The server listens on ``127.0.0.1`` only and runs until you press Ctrl-C.
Relative paths in the ``.sub`` resolve against the ``.sub`` file's own
folder first, then the directory you run the command from, as they do for
:doc:`validate` and :doc:`submit`.

``subfile``, ``autofill``, and ``validate`` take ``--web`` to finish in the same
page: ``paperpush validate biorxiv.sub.txt --web`` opens it with the checks already
run.

Options
-------

``subfile``
   A ``.sub`` file to open. It must exist and have an ``@venue:`` line;
   create one with ``paperpush subfile VENUE`` (add ``--web`` to open it
   straight away).

``-p PATH``, ``--path PATH``
   A manuscript directory whose files the page offers in its file pickers; a
   file stands for the directory holding it. Needs a ``.sub`` file. May be
   repeated. (``-d``/``--directory`` are accepted as older names.)

``--port PORT``
   The port to listen on. Default: any free port.

``--no-browser``
   Print the page's address instead of opening it in your browser.

The buttons at the bottom of the page
-------------------------------------

**Download .sub**
   The ``.sub`` file the form writes. (The file itself is also shown, and
   editable, in the collapsed *The .sub file* section above the checks.)

**Download .zip**
   One folder holding the ``.sub`` file and every file it names. Whatever is
   in the figures upload box goes in a ``figures/`` subfolder, the tables
   box in ``tables/``, and the supplementary box in ``supplementary/``;
   everything else sits beside the ``.sub``. The ``.sub`` inside names the files
   where they now are, so ``paperpush validate`` and ``paperpush submit``
   work from inside the unzipped folder, and the page can open the zip again
   later. On a ``--web`` page the files come from your computer; on the
   website, from your uploads (uploads the ``.sub`` does not name come along
   too). If a file the ``.sub`` names cannot be found, nothing is downloaded
   and the page lists the missing paths.

**Download checks**
   The last validation's report as a text file, like
   ``paperpush validate -o report.txt``. Validate first.

**HTML**
   A single file to send to a PI or collaborator. It opens in any browser
   with no paperpush needed. The side menu, filters, search, light and dark
   switch, and copy buttons still work, but the fields are read-only. A link
   at the top opens an editable copy on the website.

**Share**
   (On the hosted site.) Puts the form online and copies a link. Everyone who
   opens the link edits the same form, with the same word counts, options,
   and checks, and each edit is saved a moment after it is made. If two
   people change *different* fields at once, both changes are kept. If they
   change the *same* field, the one who saves second chooses which value to
   keep. The form is encrypted in the browser with a key that exists only in
   the link (after the ``#``, which browsers never send to a server), so the
   server stores only ciphertext. Anyone with the link can read and edit the
   form, and a lost link cannot be recovered. Uploaded files are not shared:
   each person uploads their own to validate. **Delete for everyone** removes
   the shared form; a form nobody edits for 90 days is deleted.

   On a ``--web`` page, which only runs on your computer, **Share** opens
   the form, filled in, on the paperpush website (the values travel inside
   the link, after the ``#``, so nothing is stored on a server). Press
   **Share** there to edit it with others. Uploaded files stay on your
   computer.

**Portal**
   Fills out the venue's submission portal, stopping before the final submit
   button.
