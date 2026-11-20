# paperpush security program

This folder is paperpush's information security program: the policies, the
controls that put them into effect, and where to find the evidence for each.
The controls are mapped to the SOC 2 Trust Services Criteria (2017, revised
2022) for **Security** (common criteria CC1–CC9) and **Confidentiality**
(C1). Availability and Privacy are out of scope for now. The website runs on
a free hosting plan with no uptime commitment, and it keeps no accounts.

| Document | What it covers |
| --- | --- |
| [policy.md](policy.md) | Information security policy: scope, roles, data classification, rules |
| [risk-register.md](risk-register.md) | Risk assessment, open gaps, and accepted risks |
| [change-management.md](change-management.md) | How code reaches users and the website |
| [subprocessors.md](subprocessors.md) | Vendor register: who handles what data |
| [incident-response.md](incident-response.md) | What to do when something goes wrong |
| [../SECURITY.md](../SECURITY.md) | Vulnerability disclosure |
| [Privacy page](https://paperpush.onrender.com/privacy) | Data handling, for website visitors (`paperpush/web/static/privacy.html`) |

## Control matrix

| ID | Control | TSC | Type | Frequency | Evidence |
| --- | --- | --- | --- | --- | --- |
| SEC-01 | A written security policy, owned by the lead maintainer and reviewed every year | CC1.1, CC1.3, CC2.1, CC5.3 | Preventive | Annual | `security/policy.md` and its git history |
| SEC-02 | A risk register, reviewed every year and after each incident | CC3.1–CC3.4 | Preventive | Annual | `security/risk-register.md` and its git history |
| SEC-03 | Changes reach `main` only through a reviewed pull request with CI passing (branch protection), and Render deploys only commits whose checks passed (`autoDeployTrigger: checksPass`) | CC8.1 | Preventive | Every change | GitHub branch protection settings, PR history, `render.yaml`, Render deploy log |
| SEC-04 | Uploaded workspaces are deleted 24 h after last use (swept every 15 min), shared forms 90 days after the last save, and the disk is wiped on every redeploy | C1.2 | Preventive | Continuous | `paperpush/web/app.py` (`Workspaces.sweep`, the lifespan sweeper), `shared.py`, `tests/test_web.py::test_workspaces_expire_on_a_timer_without_new_visitors` |
| SEC-05 | Static analysis (bandit, failing on medium or high severity) and secret scanning of the full git history (gitleaks) on every push, every PR, and weekly | CC7.1, CC8.1 | Detective | Every change, and weekly | `.github/workflows/security.yml` run history, `.gitleaks.toml` |
| SEC-06 | Production installs locked, hash-checked dependencies; pip-audit checks them for known CVEs on every change and weekly; the base image is pinned by digest and Actions by commit SHA; Dependabot proposes updates as PRs | CC7.1, CC8.1, CC9.2 | Preventive and detective | Every change, and weekly | `requirements/*.txt`, `Dockerfile`, `.github/dependabot.yml`, Dependabot PRs, `security.yml` runs |
| SEC-07 | Shared forms are end-to-end encrypted (AES-GCM, key only in the link fragment); write tokens are stored as SHA-256 hashes and compared in constant time; TLS everywhere, with HSTS | CC6.1, CC6.7, C1.1 | Preventive | Continuous | `paperpush/web/shared.py`, `static/shared.js`, response headers |
| SEC-08 | Website hardening: strict CSP with no inline script, `X-Frame-Options`, `nosniff`, `no-referrer`, `no-store` on API answers; workspace IDs of 192 random bits; upload, zip, and `.sub` size caps; sanitized file names; no server-side fetching of links in uploads; non-root container | CC6.1, CC6.6, CC6.8 | Preventive | Continuous | `paperpush/web/app.py` (`_SecurityHeaders`, `Workspaces`, `safe_name`), `render.yaml`, `Dockerfile`, `tests/test_web.py` |
| SEC-09 | Credentials: venue passwords go to the OS keyring through a one-time sign-in page on 127.0.0.1 and never pass through an agent; visitors' API keys are used per request, never stored, and masked in output | CC6.1, C1.1 | Preventive | Continuous | `paperpush/credentials.py`, `paperpush/login_form.py`, `app.py` (`_scrub`) |
| SEC-10 | A register of subprocessors with the data each receives, reviewed every year and before a new one is added | CC9.2 | Preventive | Annual, and on change | `security/subprocessors.md` |
| SEC-11 | An hourly uptime check on `/healthz` that emails on failure; Render's health check restarts an unhealthy instance | CC7.2 | Detective | Hourly | `.github/workflows/uptime.yml` run history, Render events |
| SEC-12 | A written incident response plan with severity levels, playbooks, and a post-incident review | CC7.3, CC7.4, CC7.5 | Corrective | On incident; plan reviewed every year | `security/incident-response.md`, incident records (GitHub issues or advisories) |
| SEC-13 | A public vulnerability disclosure policy with response targets, taking private reports | CC2.3, CC7.3 | Detective | Continuous | `SECURITY.md`, GitHub security advisories |
| SEC-14 | A public privacy and data notice on the website: retention, encryption, subprocessors | CC2.3, C1.1 | Preventive | Reviewed with SEC-10 | `/privacy`, `paperpush/web/static/privacy.html` |

Known gaps not yet covered by a control are listed in
[risk-register.md](risk-register.md#open-gaps).

## Yearly review checklist

Each November the lead maintainer goes through this list and records the
review as a commit to this folder:

- [ ] Re-read `policy.md`, `risk-register.md`, and `incident-response.md`, and update them
- [ ] Go through `subprocessors.md`: still used? terms changed? anything missing?
- [ ] Confirm branch protection on `main` still requires review and CI
- [ ] Confirm `security.yml` and `uptime.yml` ran green over the year, and follow up on any failures
- [ ] Close or re-justify every Dependabot PR older than 30 days
- [ ] Check that the privacy page matches the code (TTLs, services, logging)
