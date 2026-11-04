``paperpush codegen``
=====================

Record a venue's submission wizard as runner code.

.. code-block:: text

   usage: paperpush codegen [-h] [-v] [-q] [--venue VENUE] [--sub SUBFILE]
                            [--target {paperpush,python}] [-o OUT]
                            [--new-session] [url]

This is Playwright's ``codegen`` for people writing or fixing a venue runner. It opens
a browser on ``url`` (default: the venue's portal) with a toolbar in the top
right, and records what you do there as Playwright code. Close the browser when
you are done. The code is printed, or written to ``--output`` as you go.

Options
-------

``url``
   The page to start on. Defaults to the venue's portal, else a blank page.

``--venue VENUE``
   Reuse the venue's saved sign-in (from :doc:`login`) and offer its fields as
   variable names.

``--sub SUBFILE``
   A filled ``.sub``. Typed text, chosen options and uploaded files that match
   one of its fields are recorded as that field's variable. **Play** replays
   the ``.sub``'s values. Implies ``--venue`` from the file's ``@venue``.

``--target {paperpush,python}``
   ``paperpush`` (default) renders the body of a runner's ``submit()``:
   ``values.get(...)`` declarations, ``step()`` labels, and each upload as a
   ``page.attempt`` unit. ``python`` renders a standalone Playwright script.

``-o OUT``, ``--output OUT``
   Write the code to this file, updated after every action.

``--new-session``
   Start signed out instead of reusing the saved sign-in.

The toolbar
-----------

**Record** turns recording on and off. **Play** replays the script in a new tab
(with **Pause**, **Resume** and **Stop**). **Undo** and **Redo** edit the script, not
the page. **Variable** replaces the selected line's value (click a line in the code
panel) with a variable, or renames the selected branch. **⌖ HTML** copies one
element you pick, **Copy Page HTML** the page's HTML including its frames. **Code**
shows or hides the code panel (shown by default), **Copy Code** copies the code,
and **Save Code** saves it to a file (it asks for the path).

**⌖ Dropdown Choices** notes what a question offers: pick a dropdown, a list, or one of a
group of radio buttons (open a custom dropdown first), and its choices go into the
code as a comment, such as ``# Options of "Article type" (3):`` with one line per
choice. Nothing on the page is clicked.

**? Optional** marks part of the script as optional: press it to start the
section (the code gets ``# ---OPTIONAL---``), record the optional actions, and
press it again (now **? End Optional**) to end it (``# ---END OPTIONAL---``).
The markers are comments only; replay runs the section like any other.

Branches
--------

A branch records a choice between paths, such as the options of a "manuscript
type" question. Press **Branch** and record the first option's actions, press
**Next** and record the second option's, and so on; press **End** after the last
option, then carry on (for example, click Continue). Everything recorded between
Branch and Next (or between two Nexts, or Next and End) is one path, however
many actions it takes, and the branch renders as a ``match`` with one ``case``
per path, keyed by the label of the path's first action:

.. code-block:: python

   match server_suitability:
       case "Diagnostic tools or medical equipment tested in humans":
           page.get_by_role("radio", name="Diagnostic tools or medical equipment tested in humans").check()
       case "Biological research":
           page.get_by_role("radio", name="Biological research").check()
   page.get_by_role("button", name="Continue").click()

The branch's variable is ``branch_1``, ``branch_2``, ... until its paths match a
venue field's options, when it takes that field's name. Click the ``match`` line
or a ``case`` line in the code panel to retype the name or a path's key. **Play**
takes the path the ``.sub`` value names, else the path recorded last.

Passwords are recorded as the ``password`` variable and never written out.
