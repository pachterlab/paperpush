# Change management

*Control SEC-03. Owner: lead maintainer.*

## How a change reaches users

1. **Branch and pull request.** All work happens on a branch and arrives as a
   pull request. That includes maintainers' own changes and Dependabot updates.
2. **Automated checks** (required to merge):
   - `CI`: the test suite on Python 3.10–3.12, plus the generated-file sync checks.
   - `Security`: bandit, gitleaks, and pip-audit with the lock-file install (`security.yml`).
   - `Docs`: the Sphinx build with warnings treated as errors.
3. **Review.** At least one maintainer other than the author approves.
   Branch protection on `main` enforces both the review and the checks.
   Changes to these areas get an explicit security read:
   - `paperpush/web/` (anything a website visitor reaches),
   - `paperpush/credentials.py`, `paperpush/login_form.py`,
   - the submit guardrails (`paperpush/venues/base.py`, the recovery tools),
   - `.github/workflows/`, `Dockerfile`, `render.yaml`, `requirements/`.
4. **Merge to `main`.**
5. **Website deploy.** Render builds the Docker image for a commit on `main`
   only after its GitHub checks pass (`autoDeployTrigger: checksPass` in
   `render.yaml`). Render's deploy log records each deploy.
6. **PyPI release.** A maintainer tags a release from `main` and publishes it.
   See [DEVELOPMENT.md](../DEVELOPMENT.md).

## Emergency changes

When the website is exposing data, or a fix cannot wait for a reviewer:

1. Contain first: suspend the Render service from the dashboard. That stops
   all traffic without a code change.
2. If code must ship before review, a maintainer may merge with an admin
   override. Open a follow-up issue labelled `emergency-change` the same day,
   linking the PR. Another maintainer reviews it after the fact within 5
   business days.

## Dependency and image updates

Dependabot opens weekly PRs for `pyproject.toml`, `requirements/*.txt`, the
Actions pinned by SHA, and the Docker base-image digest. They go through the
same review and checks. To regenerate a lock file by hand, run the command
recorded at the top of that file and commit the result in a PR.
