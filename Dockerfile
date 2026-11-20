# The paperpush website (`paperpush-web`), for hosting on Render or any other
# container host. Build and run locally with:
#
#   docker build -t paperpush-web .
#   docker run -p 8000:8000 paperpush-web
#
# Settings come from PAPERPUSH_WEB_* environment variables (see README.md).
# Pinned by digest so every build starts from the same image; Dependabot
# (.github/dependabot.yml) proposes a new digest when the tag moves.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    PAPERPUSH_WEB_DATA=/data

WORKDIR /app

# Copy only what the package build needs, so edits to docs or tests don't
# invalidate the install layer.
COPY pyproject.toml README.md LICENSE ./
COPY requirements/web.txt requirements/build.txt ./requirements/

# The third-party packages come from a lock file with hashes, so production
# installs exactly the versions CI audited (pip-audit in security.yml):
# web: the server; ai: the LLM SDKs for autofill and the AI checks;
# validate: PDF layout and LaTeX checks. Playwright's browsers are not
# installed -- the website never drives a submission portal.
# The build backend (setuptools) is locked too, and paperpush itself is
# built without fetching anything. Regenerate each lock with the command at
# the top of requirements/web.txt and requirements/build.txt.
RUN pip install --require-hashes -r requirements/web.txt -r requirements/build.txt

COPY paperpush ./paperpush
RUN pip install --no-deps --no-build-isolation .

RUN useradd --create-home --uid 1000 paperpush \
    && mkdir -p /data \
    && chown paperpush:paperpush /data
USER paperpush

EXPOSE 8000

# Render (and most hosts) set PORT; main() reads it.
CMD ["paperpush-web", "--host", "0.0.0.0"]
