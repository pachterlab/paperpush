# PaperPush: Automated manuscript submission to journals, conferences, and preprint servers

<!-- [![Documentation Status](https://readthedocs.org/projects/paperpush/badge/?version=latest)](https://paperpush.readthedocs.io/en/latest/?badge=latest) -->

Prepare manuscripts for submission to preprint servers, venues, and conferences with just a few commands. No need to spend hours verifying manuscript files for journal compliance and filling out submission portal forms manually. Just provide a manuscript directory and submission venue of interest, and `paperpush` will validate the documents and fill out the submission portal for you.

## Install

```bash
pip install paperpush
```

To install the playwright dependency and the required chromium browser, run:

```bash
playwright install chromium
```

## Use with AI

```bash
git clone https://github.com/pachterlab/paperpush.git
cd paperpush
```

```LLM
Prepare my manuscript in /PATH/TO/MANUSCRIPT/DIRECTORY for submission to VENUE with PaperPush.
```

The Claude skill `/paperpush-prepare-submission` helps with this. 

See [`docs/example-session.md`](docs/example-session.md) for a full worked example of this flow.

## Quickstart
1. `paperpush subfile VENUE`: creates a file VENUE.sub that is a template for the VENUE submission. Fields are grouped into the same sections for every venue (manuscript type, files, details, authors, reviewers, declarations, additional); add `--venue-order` to list them in the order the venue's portal asks for them instead.
2. **fill out VENUE.sub - 3 options:**
   - **a.** *fill out manually*
   - **b.** *Ask an LLM*: Use Claude skill `/paperpush-autofill`, or any AI agent following [`AGENTS.md`](AGENTS.md).
   - **c.** `paperpush autofill -d /PATH/TO/MANUSCRIPT/DIRECTORY [MORE_DIRS_OR_FILES ...] --engine api VENUE.sub`: Use an LLM API (Anthropic, OpenAI, or Google; pick one with `--provider`, otherwise the first one with an API key set is used). Requires that provider's API key and `pip install "paperpush[autofill]"`. `-d` takes one or more directories and/or documents (`.tex`, `.doc`, `.docx`, `.odt`, `.rtf`, `.txt`, `.md`, `.rst`, `.pdf`), up to the next flag or the `.sub` file, and can be repeated.
3. `paperpush validate VENUE.sub`: run the pre-submission checks on the filled `VENUE.sub`, including the manuscript against the venue's author guidelines (`paperpush requirements VENUE` prints those; a `.tex` manuscript is compiled with `latexmk`/`pdflatex`, if installed, to check its page count), its page layout (template, margins, font, spacing, line/page numbers), its figures (resolution, lettering size and typeface, panel labels — in attached files or embedded in the manuscript), the order figures and tables are cited in, whether the title, abstract, authors, and keywords in the `.sub` match the manuscript, whether the word, figure, and table counts on the title page are right, and whether the cover letter is dated within the last week (`--cover-letter-max-age`) and names the right venue. It prints a colour-coded report of what passed (green), failed (red), warnings (amber), and was not checked and why (yellow; `--alt-color` for a colour-blind-friendly palette); for venues that accept an initial submission in any format (Nature, Cell, PLOS Comp Bio, PNAS, ...) it notes the rule at the top, and `--initial-submission` reports every error as a warning; `-o` also saves it as PDF (or `.html`/`.md`/`.txt`). Checks that need a reader's judgement — lettering in raster figures, the guidelines' free-text rules — run with `--ai` (an Anthropic, OpenAI, or Gemini API key), or with your own Claude Code/Codex subscription through the `paperpush-validate` skill (`--ai-tasks` / `--ai-results`); `--ai-only` reports just those checks.
4. `paperpush login VENUE`: log in to the VENUE submission portal.
5. `paperpush submit VENUE.sub`: Fill out the VENUE submission portal. Will not actually submit the manuscript. We highly recommend reviewing the submission form in the venue portal before clicking submit. If a portal has changed and a step fails partway, `--recover by-hand` pauses so you can fix that step in the browser and carry on. `--ai` has an LLM fix it instead, with the same API keys and `--provider`/`--model` as `autofill --engine api`. The model can never send the submission. A run that needed recovery ends by suggesting `paperpush recovery-report`, which drafts a redacted bug report you can choose to file, so the venue gets fixed for everyone.

Helper commands:
- `paperpush requirements VENUE`: print VENUE's author guidelines, if available.
- `paperpush add-venue --template [SLUG]` / `paperpush add-venue FILE`: write a venue file to fill in for a journal, preprint server, or conference paperpush does not support yet (or correct one with `--from SLUG`), then add it. On the website, **Add venue** fills in the same file through a form.
- `paperpush update-venues`: Update the venue data (venues.json, submission_forms.json, manuscript_requirements.json) from the remote venue-data branch.
- `paperpush options VENUE.FIELD`: print the allowed values for FIELD in VENUE's submission form.

See the YouTube tutorial [PaperPush Tutorial](https://youtu.be/ChCYoud0uBk)

## Documentation

[paperpush.readthedocs.io](https://paperpush.readthedocs.io)

## Run the whole pipeline at once

`scripts/paperpush_pipeline.py` sequentially runs the commands above — `subfile`, `autofill`, `validate`, `login`, `submit`. This allows going from a manuscript directory to a filled submission portal in just one command.

```bash
python scripts/paperpush_pipeline.py -d /PATH/TO/MANUSCRIPT/DIRECTORY --engine api VENUE
```

See `python scripts/paperpush_pipeline.py --help` for the full list of options, grouped by step.

## Website

[paperpush.onrender.com](https://paperpush.onrender.com) — prepare and validate a submission in your browser, no install needed.

## Supported venues

<!-- BEGIN SUPPORTED VENUES -->
**Preprint servers:** [arXiv](https://arxiv.org), [bioRxiv](https://www.biorxiv.org), [medRxiv](https://www.medrxiv.org)

**Journals:** [AJHG](https://www.cell.com/ajhg), [Bioinformatics](https://academic.oup.com/bioinformatics), [BMC Bioinformatics](https://link.springer.com/journal/12859), [The BMJ](https://www.bmj.com), [Cell](https://www.cell.com/cell/home), [Cell Genomics](https://www.cell.com/cell-genomics/home), [Cell Reports](https://www.cell.com/cell-reports), [Cell Systems](https://www.cell.com/cell-systems/home), [Combinatorica](https://link.springer.com/journal/493), [Discrete Mathematics](https://www.sciencedirect.com/journal/discrete-mathematics), [eLife](https://elifesciences.org), [European Radiology](https://link.springer.com/journal/330), [Genome Biology](https://genomebiology.biomedcentral.com), [Genome Medicine](https://genomemedicine.biomedcentral.com), [Genome Research](https://genome.cshlp.org), [IEEE TMI](https://ieeetmi.org/), [IEEE TNNLS](https://cis.ieee.org/publications/t-neural-networks-and-learning-systems), [IEEE TPAMI](https://www.computer.org/csdl/journal/tp), [JAMA](https://jamanetwork.com/journals/jama), [JMLR](https://www.jmlr.org), [JNM](https://jnm.snmjournals.org), [The Lancet](https://www.thelancet.com/journals/lancet/home), [Lancet Digital Health](https://www.thelancet.com/journals/landig/home), [Medical Image Analysis](https://www.sciencedirect.com/journal/medical-image-analysis), [Nature](https://www.nature.com), [Nature Biomedical Engineering](https://www.nature.com/natbiomedeng), [Nature Biotechnology](https://www.nature.com/nbt), [Nature Communications](https://www.nature.com/ncomms), [Nature Genetics](https://www.nature.com/ng), [Nature Machine Intelligence](https://www.nature.com/natmachintell), [Nature Medicine](https://www.nature.com/nm), [Nature Methods](https://www.nature.com/nmeth), [NEJM](https://www.nejm.org), [Neuron](https://www.cell.com/neuron), [npj Digital Medicine](https://www.nature.com/npjdigitalmed), [Nucleic Acids Research](https://academic.oup.com/nar), [PLOS Computational Biology](https://journal.plos.org/ploscompbiol/), [PNAS](https://www.pnas.org), [Radiology](https://pubs.rsna.org/journal/radiology), [Radiology: Artificial Intelligence](https://pubs.rsna.org/journal/ai), [Science](https://www.science.org/journal/science), [Science Advances](https://www.science.org/journal/sciadv), [Science Immunology](https://www.science.org/journal/sciimmunol), [Science Robotics](https://www.science.org/journal/scirobotics), [Science Signaling](https://www.science.org/journal/signaling), [Science Translational Medicine](https://www.science.org/journal/stm), [TMLR](https://jmlr.org/tmlr/)

**Conferences:** [ACL Rolling Review](https://aclrollingreview.org), [CVPR 2027](https://cvpr.thecvf.com/Conferences/2027)
<!-- END SUPPORTED VENUES -->

View the list on the command line with `paperpush --venues`.

Venue details (field options, limits, author-guideline rules) are updated independently of releases: paperpush fetches the latest published data at most once a day, and `paperpush update-venues` fetches it right away. New venues arrive with new releases (`pip install -U paperpush`).

For more details, see [`venues.md`](venues.md)

## Adding new venues

The quickest way is a venue file: `paperpush add-venue --template my_journal` writes `my_journal.txt`, with a section for the submission portal's form and one for the author guidelines, every key described. Fill it in (yourself, with an AI agent, or with the website's **Add venue** form), then run `paperpush add-venue my_journal.txt`, which checks it and merges it into the JSON files below. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for instructions on adding new venues. See [`DEVELOPMENT.md`](DEVELOPMENT.md) for tips on adding new venues and running unit tests.

Each venue is listed in `paperpush/venues.json` (the master list of venues and their general metadata). A venue paperpush can submit to has a submission-form entry in `paperpush/submission_forms.json` (the fields the portal asks for), and a venue paperpush can validate against has a manuscript-requirements entry in `paperpush/manuscript_requirements.json` (what the venue's author guidelines demand of the manuscript: file formats and sizes, word/page limits, required sections and statements, title-page items, figure resolution and dimensions, reference rules). A venue may have either or both, so it can be supported by `validate` without `submit` or the other way round; `paperpush --venues` and [`venues.md`](venues.md) show which. Each file has a generated JSON schema, and `paperpush validate` checks a `.sub` against the form and the requirements (a venue without a requirements entry gets only the form checks; a venue without a form gets a default one naming the manuscript files).

## MCP server

To drive paperpush from any MCP client — Claude Desktop, an IDE extension — run
it as an MCP server:

```bash
pip install 'paperpush[mcp]'
```

```json
{
  "mcpServers": {
    "paperpush": {
      "command": "paperpush-mcp"
    }
  }
}
```

The server exposes the whole pipeline as tools. See [`AGENTS.md`](AGENTS.md) for
the tool list and the contract agents should follow.

## Website

For authors who would rather not use a terminal, paperpush also runs as a website.
Pick a venue and the form fills in with every field its portal asks for and the limits
its author guidelines set. Upload the manuscript (and optionally autofill the rest from
it), then validate. Simple limits such as word and keyword counts are checked as you
type. The checks that read your files run when you press **Validate**, and the ones
that need a reader run with **Validate, extended with AI**. When you are done, download
the `.sub` (or a zip of the `.sub` plus your files) and run `paperpush submit` on it.

To pick up where you left off, open a `.sub` or that zip on the site (**Open a .sub or
.zip…**, or drop it onto the page). A zip of your manuscript folder works too: each file
goes into the upload field its name or folder points to (`main.pdf` → manuscript,
`figures/Fig1.tif` → figures, `cover_letter.docx` → cover letter, `SuppTable1.xlsx` →
supplementary tables, …). This uses keyword matching that tolerates typos, not AI, and
only fills fields that are still empty.

```bash
pip install 'paperpush[web]'
paperpush web                   # the page, served from this computer, opened in your browser
paperpush web biorxiv.sub       # the same page with biorxiv.sub filled in; edits are saved to it
```

`paperpush web` runs the page locally rather than on the hosted site, so it opens at
once and your files stay on your computer.

The buttons at the bottom of the page download the `.sub` file, a `.zip` bundle (one
folder with the `.sub` and every file it names, figures, tables, and supplements each in their own subfolder), the last
validation's report, or the page as a single read-only **HTML** file that
opens in any browser, with the side menu, filters, and search still working. **Share**
on the hosted site lets co-authors work on the form with you: everyone with the link
edits the same form (with its word counts, options, and checks), edits to different
fields merge, and the server stores it encrypted with a key that only the link holds. On a `paperpush web` page, **Share** opens the form, filled in, on the hosted site,
where you can share it for editing. **Portal** fills out the venue's submission portal. (`paperpush-web --host 0.0.0.0` serves the page for a
deployment.)

Autofill and the AI checks require a visitor to enter their provider and API key
in the page header. The key is sent only with AI requests. Set `PAPERPUSH_WEB_AI=0` to turn them off on a
public deployment, and `PAPERPUSH_WEB_NETWORK_CHECKS=0` to stop the server from fetching
the links and DOIs found in uploads. Other settings: `PAPERPUSH_WEB_DATA` (where uploads
live), `PAPERPUSH_WEB_TTL_HOURS` (default 24), `PAPERPUSH_WEB_MAX_WORKSPACE_MB` (default 500).
Shared forms are stored in the Postgres database at `PAPERPUSH_WEB_DATABASE_URL` (or
`DATABASE_URL`), e.g. a free [Neon](https://neon.tech) database; without one they go in an
SQLite file in `PAPERPUSH_WEB_DATA`, which a redeploy on a host like Render wipes.
`PAPERPUSH_WEB_SHARED_TTL_DAYS` (default 90) deletes forms nobody has edited for that long,
and `PAPERPUSH_WEB_SHARED=0` turns sharing off.

### `--web`: the page for a `.sub` file on your computer

`subfile`, `autofill`, and `validate` each take `--web`, and `paperpush web FILE.sub`
opens an existing one. Each opens that `.sub` in the same page, served from your machine on 127.0.0.1, until you press Ctrl-C. Every edit
in the form is saved straight back to the file. Paths stay real paths, relative to
the directory you ran the command in, so the file works unchanged with the other
commands.

```bash
paperpush subfile biorxiv --web                     # create biorxiv.sub, fill it in through the form
paperpush autofill -d paper/ biorxiv.sub --web      # the page autofills from paper/, then you review
paperpush validate biorxiv.sub --web [--ai]         # run the checks in the page, fix, re-validate
```

Without `--web`, `subfile` and `autofill` still end by printing a localhost link that
shows the `.sub` in the same page. The page is served by a background process, so the
command returns at once. The server stops on its own after 30 minutes with the page
closed. The link is printed only when the output is a terminal and the web extra is
installed. Set `PAPERPUSH_NO_WEB_LINK=1` to turn it off.

With `--web` and no `--values`/`--engine api`, `autofill` leaves the extraction to the
page. The page uses an API key typed into it, or one set in your environment
(`ANTHROPIC_API_KEY`, ...). Files you add in the page are saved next to the
manuscript, and the page never deletes anything on disk.

The included Dockerfile uses Python 3.12 and installs `paperpush[web,autofill,validate]`.
Build and run it with `docker build -t paperpush-web .` and `docker run -p 8000:8000 paperpush-web`.

## For AI agents

If you are an AI agent asked to submit a manuscript on the user's behalf, read
[`AGENTS.md`](AGENTS.md) — or, if you are working from a `pip install` rather
than a clone of this repo, run `paperpush --agent-guide`, which prints the same
document from the copy that shipped with the installed version. It describes how to run the full pipeline while doing
your **own** field extraction (reading the manuscript and writing a
`values.json` for the default `--engine manual`) rather than relying on the
API-backed engine. It also asks the agent to check `paperpush login --list`
before it starts, and to tell you right away if you still need to run
`paperpush login <venue>` yourself — that sign-in is interactive and belongs in
your terminal. Claude Code should prefer the `/paperpush-prepare-submission`
and `/paperpush-autofill` skills, which encode the same contract.

## License
This project is licensed under the BSD-2 License - see the [LICENSE](LICENSE) file for details.

Developed by Joe Rich
