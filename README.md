# PaperPush: Automated manuscript submission to journals, conferences, and preprint servers

<!-- [![Documentation Status](https://readthedocs.org/projects/paperpush/badge/?version=latest)](https://paperpush.readthedocs.io/en/latest/?badge=latest) -->

Prepare manuscripts for submission to preprint servers, journals, and conferences with just a few commands. No need to spend hours verifying manuscript files for journal compliance and filling out submission portal forms manually. Just provide a manuscript directory and submission venue of interest, and PaperPush will validate the documents and fill out the submission portal for you.

PaperPush provides the following services:
- A standardized system to share all data submitted to a journal/conference
- Extensive venue-specific manuscript guideline checking including section lengths, reference hallucination, figure resolution, link validation, anonymization, and much much more — up to 100 checks!
- A central database of journal/conference manuscript requirements
- Automated and fast submission portal filling with deterministic scripts
- Free, open-source, actively-maintained, and never stores your data

## tl;dr

Workflow to create the manuscript metadata (.sub) file, validate against author guidelines, and fill out the submission portal. Follow your favorite path:

### Claude Code (other AI agents listed below):
- `/plugin marketplace add pachterlab/paperpush`
- `/plugin install paperpush@paperpush`
- `/paperpush-prepare-submission Prepare my manuscript in /PATH/TO/MANUSCRIPT/DIRECTORY for submission to VENUE with PaperPush.`

### Website: 
- Visit [paperpush.onrender.com](https://paperpush.onrender.com)
- Select the VENUE (or upload a .sub file)
- Upload manuscript files and fill out the metadata fields
- Click the blue "Validate" button to validate the files and metadata against author guidelines
- Click the blue "Share" button to share the editable form with co-authors (or download and share the .sub file)
- (If available) Click the "Fill out submission portal automatically" button and follow the instructions

### CLI: 
- `paperpush subfile VENUE`  # create a .sub file
- Fill out VENUE.sub.txt
- `paperpush validate VENUE.sub.txt`  # validate the files and data in VENUE.sub.txt
- Share VENUE.sub.txt with co-authors (it can be viewed in a text editor, with `paperpush web VENUE.sub.txt`, or via upload onto [paperpush.onrender.com](https://paperpush.onrender.com))
- (If available) `paperpush login VENUE`  # log in to the VENUE submission portal
- (If available) `paperpush submit VENUE.sub.txt`  # fill out the VENUE submission portal with information in VENUE.sub.txt

## Website

[paperpush.onrender.com](https://paperpush.onrender.com): prepare a submission in your browser, validate it, and share the editable form with co-authors — no install needed. 

## Install

```bash
pip install paperpush["all"]  # Recommended — for AI API usage, MCP server, and additional manuscript validation checks
pip install paperpush  # basic CLI usage
```

(Required for `submit`): To install the playwright dependency and the required chromium browser, run:

```bash
playwright install chromium
```

## Use with AI

Install the paperpush skills for your agent, so it knows how to autofill, validate, and submit:

**Claude Code** (two separate prompts):

```
/plugin marketplace add pachterlab/paperpush
```
```
/plugin install paperpush@paperpush
```

**Codex:**

```bash
codex plugin marketplace add pachterlab/paperpush
codex plugin add paperpush@paperpush
```

**From a pip install** (any agent):

```bash
paperpush install-skills                 # Claude Code, every project (~/.claude/skills)
paperpush install-skills --agent codex   # Codex (~/.codex/skills)
paperpush install-skills --dest DIR      # any other agent's skills directory
```

Then ask:

```LLM
/paperpush-prepare-submission Prepare my manuscript in /PATH/TO/MANUSCRIPT/DIRECTORY for submission to VENUE with PaperPush.
```

See [`docs/example-session.md`](docs/example-session.md) for a full worked example of this flow.

## Quickstart
1. `paperpush subfile VENUE`: creates a file VENUE.sub.txt that is a template for the VENUE submission. Fields are grouped into the same sections for every venue (manuscript type, files, details, authors, reviewers, declarations, additional); add `--venue-order` to list them in the order the venue's portal asks for them instead.
2. **fill out VENUE.sub.txt:** Open VENUE.sub.txt in a text editor to edit manually, or run `paperpush web VENUE.sub.txt` to edit in a web browser. Use the agent skill `/paperpush-autofill My manuscript files are in /PATH/TO/MANUSCRIPT/DIRECTORY. Fill out VENUE.sub.txt` or the command `paperpush autofill --engine api -p /PATH/TO/MANUSCRIPT/DIRECTORY ... VENUE.sub.txt` (API) to automatically fill from the manuscript directory with AI. `paperpush autofill` requires the provider's API key. The AI agent may not be able to fill out all fields if some information is not in the manuscript directory.
3. `paperpush validate VENUE.sub.txt`: run the pre-submission checks on the filled `VENUE.sub.txt`. Use the agent skill `/paperpush-validate` or the CLI flag `--ai` (API) to run checks with AI. The AI checks require the provider's API key.
4. `paperpush login VENUE`: log in to the VENUE submission portal.
5. `paperpush submit VENUE.sub.txt`: Fill out the VENUE submission portal. Will not actually submit the manuscript. We highly recommend reviewing the submission form in the venue portal before clicking submit. Use the agent skill `/paperpush-portal-debug` or the CLI flag `--ai` (API) to have AI fix any issues that may arise. Ignore validation checks with the `--skip-validate` flag.

Helper commands:
- `paperpush venues`: List supported venues. Filters combine:
  - `--validate`: only venues paperpush can validate a manuscript against.
  - `--submit`: only venues that also support automated submission portal filling.
  - `-k/--keywords KW[,KW...]`: only venues tagged with any of these subject keywords (e.g. `biology,genetics`).
  - `-t/--type TYPE`: only venues of this kind (`preprint`, `journal`, or `conference`; repeatable).
  - `--columns COL[,COL...]` / `--hide-columns COL[,COL...]`: choose which columns to show (`slug`, `venue`, `keywords`, `type`, `link`, `validate`, `submit`).

  For example, `paperpush venues --submit -t journal -k biology` lists the biology journals paperpush can submit to.
- `paperpush pick-venue`: List supported venues that are a good thematic fit for your manuscript. Give it the manuscript's keywords (`pick-venue hippocampus, place cells`; a keyword can be a venue subject such as `immunology`), the manuscript itself (`pick-venue manuscript.pdf`), or its `--title` and `--abstract`, which are matched against each venue's subject keywords (`--venue-name` searches by name instead); add `--ai` (API) for an LLM's ranking with a reason for each venue. Note that this only searches the venues supported in PaperPush; and it only checks for thematic fit, not impact or likelihood of acceptance.
- `paperpush update-venues`: Update the venue data (venues.json, submission_forms.json, manuscript_requirements.json) from the remote venue-data branch.
- `paperpush requirements VENUE`: print VENUE's author guidelines, if available.
- `paperpush options VENUE.FIELD`: print the allowed values for FIELD in VENUE's submission form.
- `paperpush web VENUE.sub.txt`: open a sub file in a local deployment of the web application

See the YouTube tutorial [PaperPush Tutorial](https://youtu.be/ChCYoud0uBk)

## Documentation

[paperpush.readthedocs.io](https://paperpush.readthedocs.io)

## Supported venues

<!-- BEGIN SUPPORTED VENUES -->
**Preprint servers**

| Venue | Slug | Validate + basic .sub | Submit + full .sub |
| --- | --- | :---: | :---: |
| [arXiv](https://arxiv.org) | `arxiv` | ✅ | ✅ |
| [bioRxiv](https://www.biorxiv.org) | `biorxiv` | ✅ | ✅ |
| [medRxiv](https://www.medrxiv.org) | `medrxiv` | ✅ | ✅ |

**Journals**

| Venue | Slug | Validate + basic .sub | Submit + full .sub |
| --- | --- | :---: | :---: |
| [AJHG](https://www.cell.com/ajhg) | `ajhg` | ✅ |  |
| [Bioinformatics](https://academic.oup.com/bioinformatics) | `bioinformatics` | ✅ | ✅ |
| [BMC Bioinformatics](https://link.springer.com/journal/12859) | `bmc_bioinformatics` | ✅ | ✅ |
| [BMC Biology](https://bmcbiol.biomedcentral.com) | `bmc_biology` | ✅ |  |
| [BMC Genomics](https://bmcgenomics.biomedcentral.com) | `bmc_genomics` | ✅ |  |
| [BMC Medicine](https://bmcmedicine.biomedcentral.com) | `bmc_medicine` | ✅ |  |
| [The BMJ](https://www.bmj.com) | `bmj` | ✅ |  |
| [Cell](https://www.cell.com/cell/home) | `cell` | ✅ | ✅ |
| [Cell Genomics](https://www.cell.com/cell-genomics/home) | `cell_genomics` | ✅ | ✅ |
| [Cell Reports](https://www.cell.com/cell-reports) | `cell_reports` | ✅ |  |
| [Cell Reports Methods](https://www.cell.com/cell-reports-methods) | `cell_reports_methods` | ✅ |  |
| [Cell Stem Cell](https://www.cell.com/cell-stem-cell) | `cell_stem_cell` | ✅ |  |
| [Cell Systems](https://www.cell.com/cell-systems/home) | `cell_systems` | ✅ | ✅ |
| [Combinatorica](https://link.springer.com/journal/493) | `combinatorica` | ✅ | ✅ |
| [Communications Biology](https://www.nature.com/commsbio) | `communications_biology` | ✅ |  |
| [Discrete Mathematics](https://www.sciencedirect.com/journal/discrete-mathematics) | `discrete_mathematics` | ✅ | ✅ |
| [eLife](https://elifesciences.org) | `elife` | ✅ |  |
| [European Radiology](https://link.springer.com/journal/330) | `european_radiology` | ✅ |  |
| [Frontiers in Bioinformatics](https://www.frontiersin.org/journals/bioinformatics) | `frontiers_bioinformatics` | ✅ |  |
| [Frontiers in Genetics](https://www.frontiersin.org/journals/genetics) | `frontiers_genetics` | ✅ |  |
| [Frontiers in Immunology](https://www.frontiersin.org/journals/immunology) | `frontiers_immunology` | ✅ |  |
| [Genome Biology](https://genomebiology.biomedcentral.com) | `genome_biology` | ✅ | ✅ |
| [Genome Medicine](https://genomemedicine.biomedcentral.com) | `genome_medicine` | ✅ |  |
| [Genome Research](https://genome.cshlp.org) | `genome_research` | ✅ |  |
| [IEEE TMI](https://ieeetmi.org/) | `ieee_tmi` | ✅ |  |
| [IEEE TNNLS](https://cis.ieee.org/publications/t-neural-networks-and-learning-systems) | `ieee_tnnls` | ✅ |  |
| [IEEE TPAMI](https://www.computer.org/csdl/journal/tp) | `ieee_tpami` | ✅ |  |
| [iScience](https://www.cell.com/iscience) | `iscience` | ✅ |  |
| [JAMA](https://jamanetwork.com/journals/jama) | `jama` | ✅ |  |
| [JMLR](https://www.jmlr.org) | `jmlr` | ✅ |  |
| [JNM](https://jnm.snmjournals.org) | `jnm` | ✅ |  |
| [The Lancet](https://www.thelancet.com/journals/lancet/home) | `lancet` | ✅ |  |
| [Lancet Digital Health](https://www.thelancet.com/journals/landig/home) | `lancet_digital_health` | ✅ |  |
| [Medical Image Analysis](https://www.sciencedirect.com/journal/medical-image-analysis) | `medical_image_analysis` | ✅ |  |
| [Molecular Cell](https://www.cell.com/molecular-cell) | `molecular_cell` | ✅ |  |
| [NAR Cancer](https://academic.oup.com/narcancer) | `nar_cancer` | ✅ |  |
| [NAR Genomics and Bioinformatics](https://academic.oup.com/nargab) | `nar_genomics_bioinformatics` | ✅ |  |
| [Nature](https://www.nature.com) | `nature` | ✅ | ✅ |
| [Nature Biomedical Engineering](https://www.nature.com/natbiomedeng) | `nature_biomedical_engineering` | ✅ | ✅ |
| [Nature Biotechnology](https://www.nature.com/nbt) | `nature_biotech` | ✅ | ✅ |
| [Nature Cell Biology](https://www.nature.com/ncb) | `nature_cell_biology` | ✅ |  |
| [Nature Communications](https://www.nature.com/ncomms) | `nature_communications` | ✅ |  |
| [Nature Computational Science](https://www.nature.com/natcomputsci) | `nature_computational_science` | ✅ |  |
| [Nature Genetics](https://www.nature.com/ng) | `nature_genetics` | ✅ |  |
| [Nature Machine Intelligence](https://www.nature.com/natmachintell) | `nature_machine_intelligence` | ✅ | ✅ |
| [Nature Medicine](https://www.nature.com/nm) | `nature_medicine` | ✅ |  |
| [Nature Methods](https://www.nature.com/nmeth) | `nature_methods` | ✅ | ✅ |
| [Nature Neuroscience](https://www.nature.com/neuro) | `nature_neuroscience` | ✅ |  |
| [Nature Structural & Molecular Biology](https://www.nature.com/nsmb) | `nature_structural_molecular_biology` | ✅ |  |
| [NEJM](https://www.nejm.org) | `nejm` | ✅ |  |
| [Neuron](https://www.cell.com/neuron) | `neuron` | ✅ |  |
| [npj Digital Medicine](https://www.nature.com/npjdigitalmed) | `npj_digital_medicine` | ✅ |  |
| [Nucleic Acids Research](https://academic.oup.com/nar) | `nucleic_acids_research` | ✅ | ✅ |
| [Patterns](https://www.cell.com/patterns) | `patterns` | ✅ |  |
| [PLOS Biology](https://journals.plos.org/plosbiology/) | `plos_biology` | ✅ |  |
| [PLOS Computational Biology](https://journal.plos.org/ploscompbiol/) | `plos_compbio` | ✅ | ✅ |
| [PLOS Genetics](https://journals.plos.org/plosgenetics/) | `plos_genetics` | ✅ |  |
| [PLOS Medicine](https://journals.plos.org/plosmedicine/) | `plos_medicine` | ✅ |  |
| [PLOS ONE](https://journals.plos.org/plosone/) | `plos_one` | ✅ |  |
| [PNAS](https://www.pnas.org) | `pnas` | ✅ |  |
| [Radiology](https://pubs.rsna.org/journal/radiology) | `radiology` | ✅ |  |
| [Radiology: Artificial Intelligence](https://pubs.rsna.org/journal/ai) | `radiology_ai` | ✅ |  |
| [Science](https://www.science.org/journal/science) | `science` | ✅ | ✅ |
| [Science Advances](https://www.science.org/journal/sciadv) | `science_advances` | ✅ | ✅ |
| [Science Immunology](https://www.science.org/journal/sciimmunol) | `science_immunology` | ✅ | ✅ |
| [Science Robotics](https://www.science.org/journal/scirobotics) | `science_robotics` | ✅ | ✅ |
| [Science Signaling](https://www.science.org/journal/signaling) | `science_signaling` | ✅ | ✅ |
| [Science Translational Medicine](https://www.science.org/journal/stm) | `science_translational_medicine` | ✅ | ✅ |
| [Scientific Reports](https://www.nature.com/srep) | `scientific_reports` | ✅ |  |
| [TMLR](https://jmlr.org/tmlr/) | `tmlr` | ✅ |  |

**Conferences**

| Venue | Slug | Validate + basic .sub | Submit + full .sub |
| --- | --- | :---: | :---: |
| [ACL Rolling Review](https://aclrollingreview.org) | `acl_arr` | ✅ |  |
| [CVPR 2027](https://cvpr.thecvf.com/Conferences/2027) | `cvpr_2027` | ✅ |  |
<!-- END SUPPORTED VENUES -->

View the list on the command line with `paperpush venues`.

Venues are added (for validate) and updated (for validate and submit) daily. `paperpush update-venues` updates immediately.

New venues are added for submit with paperpush releases - `pip install -U paperpush`.

## Adding new venues

Each venue is listed in `paperpush/venues.json` (the master list of venues and their general metadata), `paperpush/manuscript_requirements.json` (the manuscript guidelines), and `paperpush/submission_forms.json` (the fields the portal asks for). The script for automatic venue login and submission lives in `/paperpush/venues/PLATFORM/VENUE.py` in a VENUE class with methods `login` and `submit`. See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`DEVELOPMENT.md`](DEVELOPMENT.md) for more details.

## What is a .sub file

The .sub file is the yaml-like text file that contains all metadata and file paths required to upload a manuscript onto a venue's submission portal. An example snippet is shown below. In this case, you would provide "/PATH/TO/MANUSCRIPT.pdf", "TITLE", and "HERE IS THE ABSTRACT".

```venue.sub.txt
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
```

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

## AI Skills

| Skill | Use it to |
| --- | --- |
| `/paperpush-prepare-submission` | go from a manuscript directory to a filled-in portal (autofill, validate, submit) |
| `/paperpush-autofill` | fill a `.sub` from the manuscript files |
| `/paperpush-validate` | run every check, including the ones that need a reader |
| `/paperpush-portal-debug` | get a stalled `paperpush submit` run unstuck |
| `/paperpush-add-venue-validate` | add or refresh a venue's author guidelines (contributors) |
| `/paperpush-add-venue` | run `/paperpush-add-venue-validate` and add `login` and  `submit` support for a venue, with tests (contributors) |

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
your terminal. Agents that support skills should install them (see
[Use with AI](#use-with-ai)); `AGENTS.md` lists what each skill does and when
to use it.

## FAQ

*How does PaperPush work?*

Supported venues are listed in `paperpush/venues.json`, their submission form data are in `paperpush/submission_forms.json`, and their author guidelines are in `paperpush/manuscript_requirements.json`. `paperpush subfile` generates a file from the form for the specified venue. The file can be filled out manually, or with the help of an LLM via `paperpush autofill`. The manuscript documents are validated with `paperpush validate`. The submission portal for a selected venue is filled out with the information from this file with a `paperpush submit`. This runs a Playwright script that is written specifically for each supported venue.


*The repository says that it supports a given venue, but it fails when I try. Why is that?*

Here are a few possibilities, in order of decreasing ease of resolution:
- Sometimes, submission portals can be a bit funny, and the solution can be as simple as retrying the command 1-2 more times.
- The submission portal is having server issues. You can try logging in to the portal manually to see if it is working, as well as on a different browser and at a different time.
- The sub file has an option that we did not anticipate. You can try running the unit test for the venue with `pytest tests/test_submit.py --run-portal -s --venue VENUE`. If this passes, then it indicates an option in the sub file is not being handled correctly. If this fails, then it indicates that the submission portal has changed since the last time the venue was tested. In either case, please open an issue with the error message.
- The submission portal has changed since the last time the venue was tested. You can run the unit test listed above to see if this is the case. If so, please open an issue with the error message.


*I do not see my venue in the supported list. Is it possible to add a venue?*

Yes! See [`CONTRIBUTING.md`](CONTRIBUTING.md) if you would like to add a journal yourself, and feel free to ask any questions in the Discussion forum. 


*Does PaperPush press submit for me?*

No. PaperPush will bring you to one of the final steps on your venue of interest, but it will never actually press submit. I always recommend viewing any compiled PDFs and entered information to ensure accuracy and acknowledgment of guidelines.


*Does every venue have a unique submission portal?*

No — in fact, many share the same type of platform. PaperPush has a unique Playwright script for each supported venue, but many venues share the same submission platform. The big ones are Editorial Manager (including the Cell family and PLOS family), ScholarOne Manuscripts, eJournalPress, Snapp, and OpenReview (for many computer science conferences). The submission script may be highly similar for venues that share the same platform, so check the folders within `paperpush/venues` to see which venues share the same submission platform. See screenshots of each of the major supported venues in [`docs/portal_screenshots`](docs/portal_screenshots) to see what each submission portal looks like.


*How long does it take to add a new journal?*

It depends on the complexity of the submission portal and your comfort with coding, but I would say roughly 1-3 hours manually. AI tools with the paperpush skills can potentially do this in under 20 minutes.


*Can I vibe code a new journal?*

Yes, with `/paperpush-add-venue` (see [`CONTRIBUTING.md`](CONTRIBUTING.md)). AI agents can navigate submission portals and write the Venue class for login and submit, but it can take a lot of tokens and may stall at times, so some human oversight helps. In my experience, LLMs can make the code a bit verbose and abstract away some of the action names, which may be harder to read/maintain in my opinion, but this is just stylistic. My favorite code style is in `arxiv.py`.


## License

This project is licensed under the BSD-2 License - see the [LICENSE](LICENSE) file for details.

Developed by Joe Rich
