# Risk register

*Owner: lead maintainer. Reviewed every year and after each incident. Last
reviewed 2026-11-18.*

Likelihood and impact are each scored 1 (low) to 3 (high). Score =
likelihood × impact.

## Risks and treatments

| # | Risk | L | I | Score | Treatment | Controls |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | Someone reads another author's uploaded manuscript on the website | 1 | 3 | 3 | Mitigate: unguessable workspace IDs, 24 h retention with a timed sweep, no listing endpoint, `no-store` and `no-referrer` | SEC-04, SEC-08 |
| R2 | A shared form is read by the host or by a database breach | 1 | 3 | 3 | Mitigate: end-to-end encryption, key only in the link fragment | SEC-07 |
| R3 | XSS on the website steals a shared-form key or an API key held in the page | 1 | 3 | 3 | Mitigate: strict CSP (no inline script), DOM building without `innerHTML`, review of web changes | SEC-08, SEC-03 |
| R4 | A malicious upload (zip bomb, path traversal, XML entities, huge files) harms the server | 2 | 2 | 4 | Mitigate: size and member caps, name sanitizing, entity refusal, non-root container | SEC-08 |
| R5 | A compromised or vulnerable dependency ships to production | 2 | 3 | 6 | Mitigate: hash-locked installs, pip-audit, Dependabot, digest and SHA pinning | SEC-06 |
| R6 | A secret is committed to the repository | 1 | 3 | 3 | Mitigate: gitleaks over the full history on every push | SEC-05 |
| R7 | An unreviewed or broken change reaches the website | 1 | 2 | 2 | Mitigate: branch protection, deploys gated on checks | SEC-03 |
| R8 | A venue password leaks from the author's machine | 1 | 3 | 3 | Mitigate: OS keyring; the password never passes through an agent. Accept the file fallback (A1). | SEC-09 |
| R9 | Prompt injection in a manuscript makes an agent skill act against the author (e.g. submit or tick attestations) | 2 | 3 | 6 | Mitigate: the hidden-text check, refusals hard-coded in the submit tools (no final submit, no unnamed uploads), skills that forbid attestations | SEC-03, AGENTS.md |
| R10 | A visitor's API key leaks through an error message or report | 1 | 2 | 2 | Mitigate: keys never stored, masked in every output | SEC-09 |
| R11 | The website is overwhelmed by abuse (unlimited workspaces or validation runs) | 2 | 2 | 4 | **Open: see G1** | — |
| R12 | A maintainer account (GitHub, Render, Neon, PyPI) is taken over | 1 | 3 | 3 | Policy requires two-factor auth. **Not verified: see G2.** | SEC-01 |
| R13 | The website is down and nobody notices | 2 | 1 | 2 | Mitigate: hourly uptime check. Accept the free plan's cold starts and lack of SLA (A2). | SEC-11 |
| R14 | A third party mishandles manuscript text sent to it | 1 | 3 | 3 | Mitigate: send only when the author asks, with their own key; vendor register and privacy notice | SEC-10, SEC-14 |

## Open gaps

| # | Gap | Why open | Review by |
| --- | --- | --- | --- |
| G1 | No rate limiting or global disk cap on the website (R11) | Deferred by the maintainer, 2026-11-18 | 2027-05-18 |
| G2 | Two-factor auth and admin access on GitHub, Render, Neon, and PyPI are not reviewed on a schedule (R12) | Deferred by the maintainer, 2026-11-18 | 2027-05-18 |

## Accepted risks

| # | Risk | Reason | Compensating controls | Review by |
| --- | --- | --- | --- | --- |
| A1 | Where no OS keyring works (some headless Linux hosts), venue credentials fall back to a plain-text JSON file under the user's config directory (`credentials.py`) | Without it paperpush cannot sign in on those hosts. Encrypting the file would need a key stored next to it, which adds little. | File mode `0600`, readable only by the user. A warning when the fallback is used. `PAPERPUSH_KEYRING=0` makes it explicit. Documented here and in `credentials.py`. | 2027-11 |
| A2 | The website runs on Render's free plan: no SLA, cold starts, and a disk wiped on every deploy | A free tool with no accounts. Availability is out of SOC 2 scope. The wiped disk also shortens retention. | Uptime monitor (SEC-11). The installed `paperpush web` works offline. | 2027-11 |
| A3 | Anyone who has a workspace ID or a shared-form link has full access to it (bearer capability, no accounts) | Accounts would mean storing personal data and passwords, which is more risk than it removes | 192-bit random IDs, short retention, `no-referrer`, IDs truncated in paperpush's logs | 2027-11 |
| A4 | Render's platform logs can record request paths, which include workspace IDs | Render's logging is outside paperpush's control | Workspaces expire within 24 h. The privacy page discloses this. | 2027-11 |
