"""The plugin instruction copies must stay regular files, in lockstep with their source.

npm packs and Claude plugin installs don't reliably preserve symlinks, so the
``plugins/*/instructions.txt`` files are REGULAR-FILE COPIES of ``CLAUDE.md.example`` —
re-synced by ``scripts/bump-version.sh`` on every release. These tests pin both halves of
that contract: real files, and byte-identical to the source they were copied from.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "CLAUDE.md.example"
PLUGINS = ("claude", "opencode", "pi")


def test_the_source_directive_exists() -> None:
    assert SOURCE.is_file(), "CLAUDE.md.example is the plugin directives' source of truth"


def test_every_plugin_carries_a_byte_identical_regular_file_copy() -> None:
    source = SOURCE.read_text()
    for name in PLUGINS:
        path = ROOT / "plugins" / name / "instructions.txt"
        assert path.exists(), f"{path} is missing — run scripts/bump-version.sh to re-sync"
        assert not path.is_symlink() and not os.path.islink(path), (
            f"{path} must be a REGULAR FILE: npm packs and Claude plugin installs don't "
            "reliably preserve symlinks, so the vendored copy would break on install"
        )
        assert path.read_text() == source, (
            f"{path} drifted from CLAUDE.md.example; re-sync it "
            "(scripts/bump-version.sh re-copies on any bump)"
        )


def test_the_pi_package_carries_the_changelog_as_a_regular_file_copy() -> None:
    # Pi reads CHANGELOG.md from the package root (package.json changelogPath) after an
    # update; npm and marketplace copies drop or dangle symlinks, as with instructions.txt.
    source = ROOT / "CHANGELOG.md"
    path = ROOT / "plugins" / "pi" / "CHANGELOG.md"
    assert not path.is_symlink(), f"{path} must be a regular file, not a symlink"
    assert path.read_bytes() == source.read_bytes(), (
        f"{path} drifted from CHANGELOG.md — scripts/bump-version.sh re-syncs it"
    )
