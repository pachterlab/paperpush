# Subprocessors and vendors

*Control SEC-10. Owner: lead maintainer. Reviewed every year and before a
new service is added. Last reviewed 2026-11-18.*

"Confidential" means unpublished manuscript content or author metadata (see
[policy.md](policy.md#data-classification)). The privacy page
(`paperpush/web/static/privacy.html`) lists the services the website uses and
must match this table.

## Website (paperpush.onrender.com)

| Vendor | Role | Data received | When | Risk tier | Assurance |
| --- | --- | --- | --- | --- | --- |
| Render | Hosting: container, disk, TLS, logs | All website traffic: uploads, forms, request paths (with workspace IDs), logs | Always | Critical | Render publishes a SOC 2 Type II report (trust center). Free plan: no SLA. |
| Neon | Postgres for shared forms | Ciphertext only; it cannot decrypt it | Share for editing | Low (it receives only encrypted data) | Neon publishes a SOC 2 Type II report |
| OpenAlex (OurResearch) | Venue search | Title, abstract, keywords | Pick a venue | Medium | Public, unauthenticated API. Treat what is sent as disclosed. |
| Anthropic, OpenAI, Google | LLM for autofill, AI checks, venue ranking | Manuscript text, figures, form fields | Only with the visitor's own key, when they ask | High | Each provider's API terms apply between it and the visitor |
| Winston AI, Originality.ai | Plagiarism scan | Manuscript text | Only with the visitor's own key, when they ask | High | The provider's terms apply between it and the visitor |
| Pangram | AI-text detection | Manuscript text | Only with the visitor's own key, when they ask | High | The provider's terms apply between it and the visitor |

## Installed package (CLI, MCP server, `paperpush web`)

These run on the author's own machine, and paperpush operates no server in
between. They are listed so authors know where data goes.

| Service | Data sent | When |
| --- | --- | --- |
| Crossref, DataCite, doi.org, arXiv, NCBI E-utilities (PubMed), OpenAlex | Reference metadata, DOIs, titles | Reference and link checks in `validate`, and `pick-venue` |
| ORCID | ORCID iDs | Looking up an author's public ORCID record |
| Linked URLs and repositories (incl. anonymous.4open.science) | An HTTP request to each link | Link checks in `validate` |
| OpenReview | Author names and IDs, with the author's stored login | OpenReview profile check for OpenReview venues |
| Venue submission portals | Credentials and the submission | `paperpush login` and `submit`, as the author directs |
| LLM, plagiarism, and AI-detection providers | As above | Only with the author's own key, when they ask |

## Development and delivery

| Vendor | Role | Data | Assurance |
| --- | --- | --- | --- |
| GitHub | Source, CI, Dependabot, advisories | Source code, CI logs, no user data | SOC 2 Type II |
| PyPI | Package distribution | Released packages | Two-factor auth required for publishing |
| Read the Docs | Documentation hosting | Public docs | — |
| Docker Hub | Base image (`python:3.12-slim`, pinned by digest) | — | Official image, pinned by digest |

## Adding a vendor

Before a change sends confidential data to a new service:

1. Add a row here with the data, the trigger, and the risk tier.
2. Update the privacy page if the website uses it.
3. Get the lead maintainer's approval in the PR.
