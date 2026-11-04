"""The agent skills: skills/ is mirrored into the package, and install-skills installs them."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from paperpush import skills
from paperpush.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAMES = sorted(p.parent.name for p in (REPO_ROOT / "skills").glob("*/SKILL.md"))


def test_package_copy_matches_skills_dir():
    result = subprocess.run([sys.executable, "scripts/sync_agent_docs.py", "--check"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + "\nRun: python scripts/sync_agent_docs.py"
    assert [s.name for s in skills.available()] == SKILL_NAMES


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_front_matter(name):
    text = (REPO_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "a skill must start with its YAML front matter"
    assert f"\nname: {name}\n" in text.split("\n---", 1)[0], "front-matter name must match the directory"
    assert skills._description(text), "missing description"


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_claude_skills_link_to_skills_dir(name):
    link = REPO_ROOT / ".claude" / "skills" / name
    assert link.is_symlink() and link.resolve() == (REPO_ROOT / "skills" / name).resolve()


def test_agents_md_lists_every_skill():
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for name in SKILL_NAMES:
        assert f"`{name}`" in agents, f"AGENTS.md's skill table is missing {name}"


def test_plugin_manifests_point_at_skills_dir():
    for manifest in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        data = json.loads((REPO_ROOT / manifest).read_text(encoding="utf-8"))
        assert data["name"] == "paperpush"
        assert data["skills"] == "./skills/"


def test_contributor_skills_exist():
    assert skills.CONTRIBUTOR_SKILLS <= set(SKILL_NAMES)


def test_install_defaults_to_author_skills(tmp_path, capsys):
    assert main(["install-skills", "--dest", str(tmp_path)]) == 0
    installed = sorted(p.parent.name for p in tmp_path.glob("*/SKILL.md"))
    assert installed == [n for n in SKILL_NAMES if n not in skills.CONTRIBUTOR_SKILLS]
    for name in installed:
        assert (tmp_path / name / "SKILL.md").read_text() == (REPO_ROOT / "skills" / name / "SKILL.md").read_text()
    assert "--all" in capsys.readouterr().out


def test_install_all_and_by_short_name(tmp_path):
    assert main(["install-skills", "--all", "--dest", str(tmp_path / "all")]) == 0
    assert sorted(p.parent.name for p in (tmp_path / "all").glob("*/SKILL.md")) == SKILL_NAMES
    assert main(["install-skills", "autofill", "--dest", str(tmp_path / "one")]) == 0
    assert [p.parent.name for p in (tmp_path / "one").glob("*/SKILL.md")] == ["paperpush-autofill"]


def test_reinstall_updates_and_uninstall_removes(tmp_path, capsys):
    assert main(["install-skills", "--dest", str(tmp_path)]) == 0
    stale = tmp_path / "paperpush-validate" / "SKILL.md"
    stale.write_text("old")
    capsys.readouterr()
    assert main(["install-skills", "--dest", str(tmp_path)]) == 0
    assert "updated paperpush-validate" in capsys.readouterr().out
    assert stale.read_text() != "old"
    assert main(["install-skills", "--uninstall", "--dest", str(tmp_path)]) == 0
    assert not list(tmp_path.iterdir())


def test_install_never_clobbers_a_foreign_skill(tmp_path, capsys):
    mine = tmp_path / "paperpush-autofill"
    mine.mkdir()
    (mine / "SKILL.md").write_text("my own")
    assert main(["install-skills", "autofill", "--dest", str(tmp_path)]) == 1
    assert (mine / "SKILL.md").read_text() == "my own"
    assert main(["install-skills", "--uninstall", "--dest", str(tmp_path)]) == 0
    assert (mine / "SKILL.md").read_text() == "my own"
    assert main(["install-skills", "autofill", "--overwrite", "--dest", str(tmp_path)]) == 0
    assert (mine / "SKILL.md").read_text() != "my own"


def test_target_dirs(tmp_path):
    assert skills.target_dir("claude") == Path("~/.claude/skills").expanduser()
    assert skills.target_dir("codex") == Path("~/.codex/skills").expanduser()
    assert skills.target_dir("claude", project=tmp_path) == (tmp_path / ".claude" / "skills").resolve()


def test_unknown_skill_is_an_error(tmp_path, capsys):
    assert main(["install-skills", "nope", "--dest", str(tmp_path)]) == 2
    assert "unknown skill" in capsys.readouterr().err
