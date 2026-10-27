# The paperpush website (`paperpush-web`), for hosting on Render or any other
# container host. Build and run locally with:
#
#   docker build -t paperpush-web .
#   docker run -p 8000:8000 paperpush-web
#
# Settings come from PAPERPUSH_WEB_* environment variables (see README.md).
FROM python:3.12-slim

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
COPY paperpush ./paperpush

# web: the server; autofill: the LLM SDKs for autofill and the AI checks;
# validate: PDF layout and LaTeX checks. Playwright's browsers are not
# installed -- the website never drives a submission portal.
RUN pip install ".[web,autofill,validate]"

RUN useradd --create-home --uid 1000 paperpush \
    && mkdir -p /data \
    && chown paperpush:paperpush /data
USER paperpush

EXPOSE 8000

# Render (and most hosts) set PORT; main() reads it.
CMD ["paperpush-web", "--host", "0.0.0.0"]
