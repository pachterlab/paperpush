*How does PaperPush work?*
Information on supported venues is in `paperpush/venues.json`. `paperpush subfile` generates a file from this for the specified venue. The file can be filled out manually, or with the help of an LLM via `paperpush autofill`. The submission portal for a selected venue is filled out with the information from this file with a `paperpush submit`. This runs a Playwright script that is written specifically for each supported venue.

*I do not see my venue in the supported list. Is it possible to add a venue?*
Yes! See [`CONTRIBUTING.md`](CONTRIBUTING.md) if you would like to add a journal yourself, and feel free to ask any questions in the Discussion forum. The recommended process involves running `Playwright codegen`, walking through all options for your journal of interest, replacing values with variables, encoding these variables in venues.json, and creating sample files for unit testing in `sample_subfiles.json`.

*Does PaperPush press submit for me?*
No. PaperPush will bring you to one of the final steps on your venue of interest, but it will never actually press submit. I always recommend viewing any compiled PDFs and entered information to ensure accuracy and acknowledgment of guidelines.

*Does every venue have a unique submission portal?*
No - in fact, many share the same type of platform. PaperPush has a unique Playwright script for each supported venue, but many venues share the same submission platform. The big ones are Editorial Manager (including the Cell family and PLOS family), ScholarOne Manuscripts, eJournalPress, Snapp, and OpenReview (for many computer science conferences). The submission script may be highly similar for venues that share the same platform, so check the folders within `paperpush/venues` to see which venues share the same submission platform. See screenshots of each of the major supported venues in [`docs/portal_screenshots`](docs/portal_screenshots) to see what each submission portal looks like.

*How long does it take to add a new journal?*
It depends on the complexity of the submission portal and your comfort with coding, but I would say roughly 1-3 hours.

*Can I vibe code a new journal?*
As of now, LLMs cannot reliably observe the state of a submission portal, which means adding a new journal requires some human oversight. However, when provided with the Playwright script outlining the steps to fill out a submission portal, LLMs can be pretty good at generating the Venue class for login and submit. In my experience, LLMs can make the code a bit verbose and abstract away some of the action names, which may be harder to read/maintain in my opinion, but this is just stylistic. My favorite code style is in arxiv.py.
