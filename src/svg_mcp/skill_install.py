"""Standalone installer for svg-mcp's packaged skill (``svg-mcp skills install|uninstall``).

Follows mcp-combiner's skills-distribution contract
(mcp-combiner ``docs/designs/skills-distribution.md``, agreed 2026-11-28):

- targets: ``claude`` = ``$CLAUDE_CONFIG_DIR/skills``, else ``~/.claude/skills``;
  ``agents`` = ``~/.agents/skills`` (Pi, OpenCode and other Agent Skills clients);
- every installed skill directory carries a ``.installed-by`` JSON marker:
  ``{owner, source, skill, version, digest, installed_at}``, where ``digest`` is the
  SHA-256 of the skill's ``_manifest`` JSON exactly as fastmcp serves it;
- ownership rules: a copy owned by ``mcp-combiner`` is left alone; a ``standalone`` copy from
  svg-mcp is updated; anything unmarked (user-authored) or from another source is skipped
  with a warning;
- writes go to a temporary sibling directory that is renamed into place, so a reader never
  sees a half-written skill.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastmcp.skills import Skill

SKILL_PACKAGE = "svg_mcp"
SKILL_PATH = "skills/svg-authoring"
SOURCE = "svg-mcp"
OWNER = "standalone"
COMBINER_OWNER = "mcp-combiner"
MARKER = ".installed-by"

Target = Literal["claude", "agents"]
TARGETS: tuple[Target, ...] = ("claude", "agents")


def target_dir(target: Target) -> Path:
    """The skills directory a target client reads."""
    if target == "claude":
        config = os.environ.get("CLAUDE_CONFIG_DIR")
        base = Path(config).expanduser() if config else Path.home() / ".claude"
        return base / "skills"
    return Path.home() / ".agents" / "skills"


def packaged_skill() -> Skill:
    """The skill folder shipped in this package."""
    return Skill.from_package(SKILL_PACKAGE, SKILL_PATH)


def manifest_json(skill: Skill) -> str:
    """The skill's ``_manifest`` exactly as fastmcp serves it (the digest's input)."""
    files = [
        {"path": path, "size": file.size, "hash": file.digest}
        for path, file in skill.manifest.files.items()
    ]
    return json.dumps({"skill": skill.name, "files": files}, indent=2)


def skill_digest(skill: Skill) -> str:
    return "sha256:" + hashlib.sha256(manifest_json(skill).encode("utf-8")).hexdigest()


def _version() -> str:
    from . import __version__  # noqa: PLC0415 — avoid an import cycle at module load

    return __version__


def read_marker(directory: Path) -> dict[str, str] | None:
    """The ``.installed-by`` marker of an installed skill, or None when absent/unreadable."""
    try:
        data = json.loads((directory / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): str(v) for k, v in data.items()}


@dataclass(frozen=True)
class Outcome:
    """What an install/uninstall did to one target."""

    action: Literal[
        "installed", "updated", "current", "removed", "absent", "combiner-managed", "skipped"
    ]
    path: Path
    message: str

    @property
    def ok(self) -> bool:
        return self.action != "skipped"


def _classify(directory: Path) -> Literal["none", "combiner", "ours", "foreign"]:
    if not directory.exists() and not directory.is_symlink():
        return "none"
    marker = read_marker(directory) if directory.is_dir() and not directory.is_symlink() else None
    if marker is None:
        return "foreign"
    if marker.get("owner") == COMBINER_OWNER:
        return "combiner"
    if marker.get("owner") == OWNER and marker.get("source") == SOURCE:
        return "ours"
    return "foreign"


def _write_atomically(skill: Skill, directory: Path, marker: dict[str, str]) -> None:
    """Stage the skill plus its marker in a temporary sibling, then rename it into place."""
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}-", dir=directory.parent))
    previous = staging / "previous"
    try:
        new = staging / "new"
        for relative, content in skill.files.items():
            path = new / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (new / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        if directory.exists():
            directory.rename(previous)
        try:
            new.rename(directory)
        except OSError:
            if previous.exists():
                previous.rename(directory)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install(target: Target, *, skill: Skill | None = None) -> Outcome:
    """Install or update the packaged skill in *target*, honouring ownership."""
    skill = skill or packaged_skill()
    directory = target_dir(target) / skill.name
    kind = _classify(directory)
    digest = skill_digest(skill)
    if kind == "combiner":
        return Outcome(
            "combiner-managed",
            directory,
            f"{directory} is managed by mcp-combiner; leaving it alone",
        )
    if kind == "foreign":
        return Outcome(
            "skipped",
            directory,
            f"{directory} exists but wasn't installed by svg-mcp (no or foreign "
            f"{MARKER} marker); not touching it. Remove it yourself to install.",
        )
    if kind == "ours":
        current = read_marker(directory) or {}
        if current.get("digest") == digest:
            return Outcome("current", directory, f"{directory} is already up to date")
    marker = {
        "owner": OWNER,
        "source": SOURCE,
        "skill": skill.name,
        "version": _version(),
        "digest": digest,
        "installed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_atomically(skill, directory, marker)
    action: Literal["installed", "updated"] = "updated" if kind == "ours" else "installed"
    return Outcome(action, directory, f"{action} {skill.name} at {directory}")


def uninstall(target: Target, *, name: str | None = None) -> Outcome:
    """Remove svg-mcp's standalone copy from *target*; never another owner's."""
    directory = target_dir(target) / (name or packaged_skill().name)
    kind = _classify(directory)
    if kind == "none":
        return Outcome("absent", directory, f"{directory} isn't installed")
    if kind == "combiner":
        return Outcome(
            "combiner-managed",
            directory,
            f"{directory} is managed by mcp-combiner; leaving it alone",
        )
    if kind == "foreign":
        return Outcome(
            "skipped",
            directory,
            f"{directory} wasn't installed by svg-mcp; not removing it",
        )
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        return Outcome("skipped", directory, f"couldn't remove {directory}: {exc}")
    return Outcome("removed", directory, f"removed {directory}")


def status(target: Target) -> str:
    """One line describing what's installed in *target*."""
    skill = packaged_skill()
    directory = target_dir(target) / skill.name
    kind = _classify(directory)
    if kind == "none":
        return f"{target}: not installed ({directory})"
    marker = read_marker(directory) or {}
    if kind == "foreign":
        return f"{target}: {directory} exists, not installed by svg-mcp"
    fresh = "up to date" if marker.get("digest") == skill_digest(skill) else "out of date"
    return (
        f"{target}: {marker.get('owner')} copy, version {marker.get('version')}, "
        f"{fresh} ({directory})"
    )


def cli(argv: list[str]) -> int:
    """``svg-mcp skills install|uninstall|status [--target claude|agents|all]``."""
    import argparse  # noqa: PLC0415 — CLI-only

    parser = argparse.ArgumentParser(
        prog="svg-mcp skills",
        description="Install svg-mcp's svg-authoring skill into a client's skills directory.",
    )
    parser.add_argument("action", choices=["install", "uninstall", "status"])
    parser.add_argument(
        "--target",
        choices=[*TARGETS, "all"],
        default="all",
        help="claude ($CLAUDE_CONFIG_DIR/skills or ~/.claude/skills), "
        "agents (~/.agents/skills), or all (default)",
    )
    args = parser.parse_args(argv)
    targets: tuple[Target, ...] = TARGETS if args.target == "all" else (args.target,)
    if args.action == "status":
        for target in targets:
            print(status(target))
        return 0
    failed = False
    for target in targets:
        outcome = install(target) if args.action == "install" else uninstall(target)
        print(f"{target}: {outcome.message}")
        failed = failed or not outcome.ok
    return 1 if failed else 0
