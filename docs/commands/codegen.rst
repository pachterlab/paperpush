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
(with **Pause**, **Resume** and **Stop**). **⏏ Load** asks for the path of code
saved earlier with **Save Code** (either ``--target``), reads it back in place of
the current script, and plays it; **Undo** brings the old script back, and what you
record afterwards is added to the loaded one. Values the code's ``values`` dict
declares (``python`` target) are replayed unless the ``.sub`` gives the field;
code from the ``paperpush`` target needs ``--sub`` for its variables. Only code
codegen writes can be loaded: a hand-written ``if`` or loop is refused, naming
its line. **Undo** and **Redo** edit the script, not
the page. **Variable** replaces the selected line's value (click a line in the code
panel) with a variable, or renames the selected branch. **⌖ HTML** copies one
element you pick, **Copy Page HTML** the page's HTML including its frames. **Copy Code**
copies the code, and **Save Code** saves it to a file (it asks for the path). The
buttons in the code panel's top-left corner hide or show the code (**▾ Hide code**
/ **▸ Show code**; shown by default) and move it into a separate window
(**⧉ Window**; nothing done there is recorded); **⧈ Embed** in that window, or
closing it, puts the code back in the toolbar.

**⌖ Dropdown Choices** notes what a question offers: pick a dropdown, a list, or one of a
group of radio buttons, and its choices go into the
code as a comment, such as ``# Options of "Article type" (3):`` with one line per
choice. Nothing on the page is clicked. Custom dropdowns work too, including the
Semantic UI search dropdowns of InvenioRDM portals (CaltechDATA, Zenodo); one
whose options load only as you type needs a search typed into it first.

**? Optional** marks part of the script as optional: press it to start the
section (the code gets ``# ---OPTIONAL---``), record the optional actions, and
press it again (now **? End Optional**) to end it (``# ---END OPTIONAL---``).
The markers are comments only; replay runs the section like any other.

**# Comment** (next to **? Optional**) adds a comment at the end of a line:
click a line in the code panel (else it takes the line recorded last), type the
comment, and the line becomes ``line_of_code  # comment``. Press it again to
change the comment; leave it empty to remove it. Comments survive **Save Code**
and **Load**.

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

Branches nest. Press **Branch** inside a path to open a branch within it, even
before the path has any action (the nested branch then starts the path, which
takes its key from the first action recorded inside it). While branches are
open the button shows how many, **⑂ Branch (1)**, **⑂ Branch (2)**, and **Next**
and **End** act on the innermost one. A nested branch renders as a ``match``
inside the outer ``case``:

.. code-block:: python

   match article_type:
       case "Research article":
           page.get_by_role("radio", name="Research article").check()
           match has_data:
               case "Yes":
                   page.get_by_role("radio", name="Yes").check()
               case "No":
                   page.get_by_role("radio", name="No").check()
       case "Review":
           page.get_by_role("radio", name="Review").check()

Passwords are recorded as the ``password`` variable and never written out.
