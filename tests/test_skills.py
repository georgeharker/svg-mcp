"""The svg-authoring skill: served over MCP (SEP-2640 resources + skills/list) and installed by
the standalone installer under mcp-combiner's skills-distribution contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.skills import SkillsClientExtension

from svg_mcp import skill_install
from svg_mcp.server import _INSTRUCTIONS, mcp

SKILL = "svg-authoring"


@pytest.fixture()
def homes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point both install targets into a throwaway home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return tmp_path


# --- served over MCP ------------------------------------------------------------


def test_the_skill_is_served_as_resources() -> None:
    async def scenario() -> tuple[list[str], str, str]:
        async with Client(mcp) as client:
            uris = [str(r.uri) for r in await client.list_resources()]
            skill_md = await client.read_resource(f"skill://{SKILL}/SKILL.md")
            manifest = await client.read_resource(f"skill://{SKILL}/_manifest")
        return uris, getattr(skill_md[0], "text", ""), getattr(manifest[0], "text", "")

    uris, skill_md, manifest = asyncio.run(scenario())
    assert f"skill://{SKILL}/SKILL.md" in uris
    assert f"skill://{SKILL}/references/diagrams.md" in uris
    assert skill_md.startswith("---\nname: svg-authoring\n")
    # The installer's digest is defined over exactly this _manifest text.
    assert manifest == skill_install.manifest_json(skill_install.packaged_skill())


def test_skills_list_on_a_modern_session() -> None:
    async def scenario() -> list[str]:
        async with Client(mcp, extensions=[SkillsClientExtension()]) as client:
            return [str(s.uri) for s in await client.list_skills()]

    assert asyncio.run(scenario()) == [f"skill://{SKILL}/SKILL.md"]


def test_every_reference_the_skill_links_exists() -> None:
    skill = skill_install.packaged_skill()
    body = skill.files["SKILL.md"].decode()
    linked = set(re.findall(r"\]\((references/[a-z-]+\.md)\)", body))
    assert linked == {p for p in skill.files if p.startswith("references/")}


def test_instructions_are_a_short_preamble_pointing_at_the_skill() -> None:
    assert len(_INSTRUCTIONS) < 2000
    assert f"skill://{SKILL}/SKILL.md" in _INSTRUCTIONS
    assert f"skill://svg-mcp/{SKILL}/SKILL.md" in _INSTRUCTIONS  # behind mcp-combiner


def test_the_skill_names_only_css_style_keys() -> None:
    skill = skill_install.packaged_skill()
    text = "\n".join(content.decode() for content in skill.files.values())
    for snake in ("stroke_width", "font_size", "font_family", "text_anchor", "fill_opacity"):
        assert snake not in text, snake


# --- standalone installer --------------------------------------------------------


def _marker(directory: Path) -> dict[str, str]:
    data: dict[str, str] = json.loads((directory / ".installed-by").read_text())
    return data


def test_install_writes_the_skill_and_its_marker(homes: Path) -> None:
    roots: dict[skill_install.Target, Path] = {
        "claude": homes / "claude" / "skills",
        "agents": homes / ".agents" / "skills",
    }
    for target, root in roots.items():
        outcome = skill_install.install(target)
        assert outcome.action == "installed" and outcome.path == root / SKILL
        assert (root / SKILL / "SKILL.md").is_file()
        assert (root / SKILL / "references" / "charts.md").is_file()
        marker = _marker(root / SKILL)
        manifest = skill_install.manifest_json(skill_install.packaged_skill())
        assert marker["owner"] == "standalone" and marker["source"] == "svg-mcp"
        assert marker["skill"] == SKILL
        assert marker["digest"] == "sha256:" + hashlib.sha256(manifest.encode()).hexdigest()
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", marker["installed_at"])
        assert [p.name for p in root.iterdir()] == [SKILL]  # no staging left behind


def test_claude_target_falls_back_to_dot_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert skill_install.target_dir("claude") == tmp_path / ".claude" / "skills"


def test_reinstall_is_a_no_op_when_current_and_updates_when_stale(homes: Path) -> None:
    skill_install.install("claude")
    assert skill_install.install("claude").action == "current"
    directory = homes / "claude" / "skills" / SKILL
    marker = _marker(directory)
    marker["digest"] = "sha256:" + "0" * 64
    (directory / ".installed-by").write_text(json.dumps(marker))
    (directory / "stray.md").write_text("old file")
    assert skill_install.install("claude").action == "updated"
    assert not (directory / "stray.md").exists()  # replaced wholesale, not merged


def test_a_combiner_owned_copy_is_left_alone(homes: Path) -> None:
    directory = homes / "claude" / "skills" / SKILL
    directory.mkdir(parents=True)
    marker = {"owner": "mcp-combiner", "source": "svg-mcp", "skill": SKILL}
    (directory / ".installed-by").write_text(json.dumps(marker))
    (directory / "SKILL.md").write_text("combiner copy")
    assert skill_install.install("claude").action == "combiner-managed"
    assert skill_install.uninstall("claude").action == "combiner-managed"
    assert (directory / "SKILL.md").read_text() == "combiner copy"


@pytest.mark.parametrize(
    "marker",
    [None, {"owner": "standalone", "source": "cribsheet", "skill": SKILL}],
    ids=["unmarked", "other-source"],
)
def test_unmarked_or_foreign_copies_are_skipped(homes: Path, marker: dict[str, str] | None) -> None:
    directory = homes / "claude" / "skills" / SKILL
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("someone else's")
    if marker is not None:
        (directory / ".installed-by").write_text(json.dumps(marker))
    install = skill_install.install("claude")
    remove = skill_install.uninstall("claude")
    assert install.action == remove.action == "skipped" and not install.ok
    assert (directory / "SKILL.md").read_text() == "someone else's"


def test_uninstall_removes_only_our_copy(homes: Path) -> None:
    assert skill_install.uninstall("agents").action == "absent"
    skill_install.install("agents")
    assert skill_install.uninstall("agents").action == "removed"
    assert not (homes / ".agents" / "skills" / SKILL).exists()


def test_cli(homes: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert skill_install.cli(["install"]) == 0
    assert skill_install.cli(["status", "--target", "agents"]) == 0
    out = capsys.readouterr().out
    assert "claude: installed" in out and "agents: installed" in out
    assert "agents: standalone copy" in out and "up to date" in out
    (homes / "claude" / "skills" / SKILL / ".installed-by").unlink()
    assert skill_install.cli(["install", "--target", "claude"]) == 1  # now unmarked: skipped
