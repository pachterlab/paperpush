# PaperPush: Automated manuscript submission to journals, conferences, and preprint servers

Prepare manuscripts for submission to preprint servers, venues, and conferences with just a few commands. No need to spend hours filling out forms manually. Just provide a manuscript directory and submission venue of interest, and `paperpush` will fill out the submission portal for you.

## Install

```bash
pip install paperpush
```

To install the playwright dependency and the required browsers, run:

```bash
playwright install
```

## Use with AI
```LLM
Prepare my manuscript in PATH/TO/MANUSCRIPT/DIRECTORY for submission to VENUE with PaperPush.
```

The Claude skill `/paperpush-prepare-submission` helps with this. 

See [`docs/example-session.md`](docs/example-session.md) for a full worked example of this flow.

## Quickstart
1. `paperpush subfile VENUE`: creates a file VENUE.sub that is a template for the VENUE submission.
2. **fill out VENUE.sub - 3 options:**
  a. *fill out manually*
  b. *Ask an LLM*: Use Claude skill `/paperpush-autofill`, or any AI agent following [`AGENTS.md`](AGENTS.md).
  c. `paperpush autofill -d MANUSCRIPT_DIR --engine api VENUE.sub`: Use an LLM API. Requires an API key.
3. `paperpush validate VENUE.sub`: run the pre-submission checks on the filled `VENUE.sub`.
4. `paperpush login VENUE`: log in to the VENUE submission system
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

**Journals:** [Bioinformatics](https://academic.oup.com/bioinformatics), [BMC Bioinformatics](https://link.springer.com/venue/12859), [Cell](https://www.cell.com/cell/home), [Cell Genomics](https://www.cell.com/cell-genomics/home), [Cell Systems](https://www.cell.com/cell-systems/home), [Discrete Mathematics](https://www.sciencedirect.com/journal/discrete-mathematics), [Genome Biology](https://genomebiology.biomedcentral.com), [Nature](https://www.nature.com), [Nature Biotechnology](https://www.nature.com/nbt), [Nature Methods](https://www.nature.com/nmeth), [Nucleic Acids Research](https://academic.oup.com/nar), [PLOS Computational Biology](https://venues.plos.org/ploscompbiol/), [Science](https://www.science.org/journal/science), [Science Advances](https://www.science.org/journal/sciadv), [Science Immunology](https://www.science.org/journal/sciimmunol), [Science Robotics](https://www.science.org/journal/scirobotics), [Science Signaling](https://www.science.org/journal/signaling), [Science Translational Medicine](https://www.science.org/journal/stm)

**Conferences:** [AAAI 2027](https://aaai.org/conference/aaai/aaai-27/)
<!-- END SUPPORTED VENUES -->

View the list on the command line with `paperpush --venues`.

For more details, see [`venues.md`](venues.md)

## Adding new venues

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for instructions on adding new venues. See [`DEVELOPMENT.md`](DEVELOPMENT.md) for tips on adding new venues and running unit tests.

## For AI agents

If you are an AI agent asked to submit a manuscript on the user's behalf, read
[`AGENTS.md`](AGENTS.md). It describes how to run the full pipeline while doing
your **own** field extraction (reading the manuscript and writing a
`values.json` for the default `--engine manual`) rather than relying on the
API-backed engine. Claude Code should prefer the `/paperpush-prepare-submission`
and `/paperpush-autofill` skills, which encode the same contract.

## License
This project is licensed under the BSD-2 License - see the [LICENSE](LICENSE) file for details.

Developed by Joe Rich
