Filling the submission portal
=============================

For :doc:`supported venues <venues>`, :doc:`commands/submit` fills in the
venue's submission portal from a ``.sub`` file. Each venue has a runner, a
deterministic `Playwright <https://playwright.dev/python/>`_ script that drives
a real Chromium browser through the portal's wizard: it signs in with the
login stored by :doc:`commands/login`, types in the title, abstract, authors,
and declarations, uploads each file, and answers the portal's questions. The
browser stays open on one of the last pages of the wizard so you can review
everything and press the final submit button yourself. PaperPush never sends
the submission.

Portals change, so a step can fail when a button is renamed, a question is
added, or an upload is slow. Instead of failing, the run pauses at that step.
The ``paperpush-portal-debug`` AI skill then takes over: it reads the live page,
gets the run past the failing step (``submit_page``, ``submit_act``, and
``submit_resume`` over MCP), and logs each change the runner needs. In a source
checkout it also puts those changes into the runner with a regression test, so
the next run does not stall at the same step. Like ``submit``, the skill can
never press the final submit button. See :doc:`commands/recovery-report` to
report a fix so the venue's runner is updated for everyone.

.. raw:: html

   <video autoplay loop muted playsinline controls style="width: 100%; max-width: 900px;">
     <source src="_static/submit_video.mp4" type="video/mp4">
     Your browser does not support the video tag.
   </video>
