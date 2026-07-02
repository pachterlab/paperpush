# Git hooks

Version-controlled git hooks for this repository.

## Activation

Hooks here are not active until git is told to use this directory. Run once
per clone:

```bash
git config core.hooksPath .githooks
```

(`core.hooksPath` is a local setting and is not copied by `git clone`.)

## `pre-commit`

Runs two best-effort scanners on staged content. Each is skipped (with a
notice) when its tool is not installed, so neither is a hard dependency.

### gitleaks — secret detection

[gitleaks](https://github.com/gitleaks/gitleaks) scans all staged files of
any type (`.py`, `.env`, `.yaml`, `.json`, ...) for API keys, tokens, private
keys, and high-entropy strings by pattern. This covers the secrets bandit's
name-based checks miss (e.g. `API_KEY = "sk-..."`).

Install (single binary, no package manager required):

```bash
# from https://github.com/gitleaks/gitleaks/releases
curl -sSL -o /tmp/gitleaks.tar.gz \
  https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_x64.tar.gz
tar xzf /tmp/gitleaks.tar.gz -C ~/.local/bin gitleaks   # ~/.local/bin must be on PATH
```

(macOS: `brew install gitleaks`.)

### bandit — Python static analysis

[bandit](https://bandit.readthedocs.io/) scans staged Python source and
aborts the commit on:

- hardcoded passwords / secrets (bandit `B105`, `B106`, `B107`);
- any medium-or-higher severity and medium-or-higher confidence finding
  (e.g. SQL injection `B608`, shell injection, unsafe deserialization).

Test fixtures and generated trees (`tests/`, `build/`, `notebooks/`,
`trash/`, `data/`, `*.egg-info/`) are not scanned. Install with:

```bash
pip install -e ".[dev]"
```

### Escape hatches

- gitleaks: add an inline `gitleaks:allow` comment on an audited line, or
  register the finding in a `.gitleaksignore` file;
- bandit: annotate an audited false positive with a trailing `# nosec`;
- either: bypass the whole hook for one commit with `git commit --no-verify`.
