# PaperPush: Automated manuscript submission to journals, conferences, and preprint servers

<!-- [![Documentation Status](https://readthedocs.org/projects/paperpush/badge/?version=latest)](https://paperpush.readthedocs.io/en/latest/?badge=latest) -->

Prepare manuscripts for submission to preprint servers, venues, and conferences with just a few commands. No need to spend hours filling out forms manually. Just provide a manuscript directory and submission venue of interest, and `paperpush` will fill out the submission portal for you.

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
git clone https://github.com/paperpush/paperpush.git
cd paperpush
```

```LLM
Prepare my manuscript in /PATH/TO/MANUSCRIPT/DIRECTORY for submission to VENUE with PaperPush.
```

The Claude skill `/paperpush-prepare-submission` helps with this. 

See [`docs/example-session.md`](docs/example-session.md) for a full worked example of this flow.

### MCP server

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

## Quickstart
1. `paperpush subfile VENUE`: creates a file VENUE.sub that is a template for the VENUE submission.
2. **fill out VENUE.sub - 3 options:**
   - **a.** *fill out manually*
   - **b.** *Ask an LLM*: Use Claude skill `/paperpush-autofill`, or any AI agent following [`AGENTS.md`](AGENTS.md).
   - **c.** `paperpush autofill -d /PATH/TO/MANUSCRIPT/DIRECTORY --engine api VENUE.sub`: Use an LLM API. Requires an API key.
3. `paperpush validate VENUE.sub`: run the pre-submission checks on the filled `VENUE.sub`.
4. `paperpush login VENUE`: log in to the VENUE submission portal. Add `--orcid` to sign in with your ORCID iD and ORCID password instead of a VENUE account, for venues whose portal offers "Sign in with ORCID" (bioRxiv, medRxiv, arXiv, the Cell Press journals, PLOS Computational Biology, BMC Bioinformatics, and Genome Biology so far).
5. `paperpush submit VENUE.sub`: Fill out the VENUE submission portal. Will not actually submit the manuscript. We highly recommend reviewing the submission form in the venue portal before clicking submit.

## Run the whole pipeline at once

`scripts/paperpush_pipeline.py` sequentially runs the commands above — `subfile`, `autofill`, `validate`, `login`, `submit`. This allows going from a manuscript directory to a filled submission portal in just one command.

```bash
python scripts/paperpush_pipeline.py -d /PATH/TO/MANUSCRIPT/DIRECTORY --engine api VENUE
```

See `python scripts/paperpush_pipeline.py --help` for the full list of options, grouped by step.

## Supported venues

<!-- BEGIN SUPPORTED VENUES -->
**Preprint servers:** [arXiv](https://arxiv.org), [bioRxiv](https://www.biorxiv.org), [medRxiv](https://www.medrxiv.org)

**Journals:** [Bioinformatics](https://academic.oup.com/bioinformatics), [BMC Bioinformatics](https://link.springer.com/journal/12859), [Cell](https://www.cell.com/cell/home), [Cell Genomics](https://www.cell.com/cell-genomics/home), [Cell Systems](https://www.cell.com/cell-systems/home), [Discrete Mathematics](https://www.sciencedirect.com/journal/discrete-mathematics), [Genome Biology](https://genomebiology.biomedcentral.com), [Nature](https://www.nature.com), [Nature Biotechnology](https://www.nature.com/nbt), [Nature Methods](https://www.nature.com/nmeth), [Nucleic Acids Research](https://academic.oup.com/nar), [PLOS Computational Biology](https://journal.plos.org/ploscompbiol/), [Science](https://www.science.org/journal/science), [Science Advances](https://www.science.org/journal/sciadv), [Science Immunology](https://www.science.org/journal/sciimmunol), [Science Robotics](https://www.science.org/journal/scirobotics), [Science Signaling](https://www.science.org/journal/signaling), [Science Translational Medicine](https://www.science.org/journal/stm)

**Conferences:** _none yet_
<!-- END SUPPORTED VENUES -->

View the list on the command line with `paperpush --venues`.

For more details, see [`venues.md`](venues.md)

## Documentation

[paperpush.readthedocs.io](https://paperpush.readthedocs.io)

## Adding new venues

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for instructions on adding new venues. See [`DEVELOPMENT.md`](DEVELOPMENT.md) for tips on adding new venues and running unit tests.

## For AI agents

If you are an AI agent asked to submit a manuscript on the user's behalf, read
[`AGENTS.md`](AGENTS.md) — or, if you are working from a `pip install` rather
than a clone of this repo, run `paperpush agent-guide`, which prints the same
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
