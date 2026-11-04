"""Install paperpush's agent skills into an agent's skills directory.

The skills live in ``skills/`` at the repo root and are mirrored into
``paperpush/_skills/`` (by ``scripts/sync_agent_docs.py``) so they ship with
the wheel. ``paperpush install-skills`` copies them from there into, by
default, ``~/.claude/skills``, where Claude Code finds them in every project.

Skills for authors are installed by default. The contributor skills, which
add or fix venues and need a source checkout, are installed with ``--all`` or
by name.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

PACKAGE_SKILLS_DIR = Path(__file__).resolve().parent / "_skills"

# Skills that edit paperpush itself (venue data, runners, tests). They need a
# clone of the repo, so a plain install of the author skills leaves them out.
CONTRIBUTOR_SKILLS = frozenset({"paperpush-add-venue", "paperpush-add-venue-validate"})

# Where each agent looks for user-level skills.
AGENT_DIRS = {
    "claude": Path("~/.claude/skills"),
    "codex": Path("~/.codex/skills"),
}
# Where each agent looks for project-level skills, relative to the project root.
PROJECT_DIRS = {
    "claude": Path(".claude/skills"),
    "codex": Path(".codex/skills"),
}

# Written into each installed skill directory, so uninstall and a later
# install only ever touch directories this command created.
MARKER = ".paperpush-skill"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the packaged SKILL.md

    @property
    def contributor(self) -> bool:
        return self.name in CONTRIBUTOR_SKILLS


def _description(text: str) -> str:
    """The ``description`` from a SKILL.md's front matter, folded to one line."""
    match = re.match(r"---\n(.*?)\n---", text, re.S)
    if not match:
        return ""
    lines = match.group(1).splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value and value not in (">-", ">", "|", "|-"):
            return value
        folded = []
        for cont in lines[i + 1 :]:
            if cont and not cont.startswith(" "):
                break
            folded.append(cont.strip())
        return " ".join(part for part in folded if part)
    return ""


def available(source: Path | None = None) -> list[Skill]:
    """Every skill that ships with this version, sorted by name."""
    root = source or PACKAGE_SKILLS_DIR
    return [Skill(p.parent.name, _description(p.read_text(encoding="utf-8")), p) for p in sorted(root.glob("*/SKILL.md"))]


def select(skills: list[Skill], names: list[str] | None, *, include_contributor: bool) -> list[Skill]:
    """The skills to act on: the named ones, else the author skills (plus contributor ones if asked)."""
    if names:
        by_name = {s.name: s for s in skills}
        # Accept the short form too: `autofill` for `paperpush-autofill`.
        wanted = [n if n in by_name else f"paperpush-{n}" for n in names]
        unknown = [n for n, w in zip(names, wanted) if w not in by_name]
        if unknown:
            raise ValueError(f"unknown skill(s): {', '.join(unknown)}; choose from {', '.join(by_name)}")
        return [by_name[w] for w in dict.fromkeys(wanted)]
    return [s for s in skills if include_contributor or not s.contributor]


def target_dir(agent: str = "claude", *, project: str | os.PathLike | None = None, dest: str | os.PathLike | None = None) -> Path:
    """The skills directory to install into."""
    if dest:
        return Path(dest).expanduser().resolve()
    if project is not None:
        return (Path(project).expanduser() / PROJECT_DIRS[agent]).resolve()
    return AGENT_DIRS[agent].expanduser()


def install(skills: list[Skill], directory: Path, *, overwrite: bool = False) -> list[tuple[Skill, str]]:
    """Copy each skill into ``directory/<name>/``. Returns (skill, outcome) pairs.

    An outcome is ``installed``, ``updated``, ``unchanged``, or ``skipped``. A
    skill is skipped when its directory exists and was not put there by this
    command (no marker), unless ``overwrite`` is set.
    """
    results = []
    for skill in skills:
        dest = directory / skill.name
        new_text = skill.path.read_text(encoding="utf-8")
        if dest.is_symlink() or (dest.exists() and not (dest / MARKER).exists()):
            if not overwrite:
                results.append((skill, "skipped"))
                continue
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
            else:
                shutil.rmtree(dest)
        existing = dest / "SKILL.md"
        if existing.is_file() and existing.read_text(encoding="utf-8") == new_text:
            results.append((skill, "unchanged"))
            continue
        outcome = "updated" if existing.is_file() else "installed"
        dest.mkdir(parents=True, exist_ok=True)
        existing.write_text(new_text, encoding="utf-8")
        (dest / MARKER).write_text("Installed by `paperpush install-skills`; `paperpush install-skills --uninstall` removes it.\n", encoding="utf-8")
        results.append((skill, outcome))
    return results


def uninstall(skills: list[Skill], directory: Path) -> list[tuple[Skill, str]]:
    """Remove each skill this command installed. Returns (skill, outcome) pairs.

    An outcome is ``removed``, ``absent``, or ``kept`` (the directory exists but
    was not installed by this command, so it is left alone).
    """
    results = []
    for skill in skills:
        dest = directory / skill.name
        if not dest.exists() and not dest.is_symlink():
            results.append((skill, "absent"))
        elif dest.is_symlink() or not (dest / MARKER).exists():
            results.append((skill, "kept"))
        else:
            shutil.rmtree(dest)
            results.append((skill, "removed"))
    return results
