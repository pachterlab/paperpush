# Information security policy

*Owner: the lead maintainer (see `maintainers` in `pyproject.toml`). Reviewed
every year in November. Last reviewed 2026-11-18.*

## Purpose and scope

paperpush handles unpublished manuscripts, author metadata, and credentials
for venue submission systems. This policy covers:

- the open-source `paperpush` package (CLI, MCP server, agent skills, venue runners),
- the public website at paperpush.onrender.com and its database,
- the accounts that build, host, and publish them (GitHub, Render, Neon, PyPI,
  Read the Docs).

It applies to everyone with write access to the repository or admin access to
those accounts ("maintainers"). Outside contributors follow
[CONTRIBUTING.md](../CONTRIBUTING.md). Their changes go through a maintainer's
review (see [change-management.md](change-management.md)).

## Roles

- **Lead maintainer:** owns this policy, the risk register, and the vendor
  register. Approves new subprocessors. Is the incident lead unless they
  delegate it.
- **Maintainers:** review pull requests against this policy, respond to
  security reports, and keep their own accounts secure.

## Data classification

| Class | Examples | Rules |
| --- | --- | --- |
| **Secret** | Venue passwords, visitors' API keys, `PAPERPUSH_WEB_DATABASE_URL`, PyPI tokens | Never committed, logged, or sent to an AI agent. Stored only in the OS keyring, the host's secret store, or the visitor's browser. Masked in any output. |
| **Confidential** | Uploaded manuscripts, figures, `.sub` content, review comments | Kept only as long as stated on the privacy page. Sent to a third party only when the author asks, with the author's own key, or as listed in `subprocessors.md`. Never used for anything but the requested task. |
| **Internal** | Server logs, CI logs | No secrets or file contents. IDs truncated to 6 characters. |
| **Public** | Source code, venue databases, docs | Reviewed before publishing (see change management). |

## Rules

1. **Least data.** The website keeps no accounts and stores uploads only for
   the stated retention. New features must not add storage of confidential
   data without updating the privacy page and `subprocessors.md`.
2. **Secrets.** Never commit a secret. gitleaks scans every push. A secret
   that reaches git is treated as leaked: rotate it, then clean up the history
   (see the incident plan).
3. **Changes.** Every change to `main` comes through a reviewed pull request
   with CI passing. Production deploys only commits whose checks passed.
4. **Dependencies.** Production dependencies are locked with hashes, audited
   for known CVEs on every change and weekly, and updated through Dependabot
   PRs. A known vulnerability is fixed within 30 days for critical and high
   severity, 90 days otherwise, or recorded as an accepted risk.
5. **Accounts.** Maintainers use a unique password and two-factor
   authentication on GitHub, Render, Neon, and PyPI. Access is removed when
   someone stops maintaining.
6. **Third parties.** A new service that will receive confidential data needs
   the lead maintainer's approval and an entry in `subprocessors.md` before it
   ships.
7. **Authors stay in control.** paperpush never sends a submission, ticks an
   attestation, or enters a password on the author's behalf (see AGENTS.md).
   Changes that weaken these guardrails need explicit review as security
   changes.
8. **Incidents.** Anyone who suspects an incident follows
   [incident-response.md](incident-response.md) right away.

## Exceptions

Exceptions are recorded as accepted risks in
[risk-register.md](risk-register.md), with a reason and a review date.
