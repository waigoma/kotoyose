"""CHANGELOG sections used as GitHub Release notes."""

import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release-notes.py"
_SPEC = importlib.util.spec_from_file_location("release_notes", ROOT_SCRIPT)
release_notes_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_notes_module)
release_notes = release_notes_module.release_notes
highest_semver = release_notes_module.highest_semver

CHANGELOG = """\
# Kotoyose

## Unreleased

- unreleased only

## 1.2.0

- undated only
- undated second

### Kept

- subsection stays

## 1.1.0 (2026-11-21)

- dated only
- dated second

## 1.10.0

- ten only

## 1.0.0 (2026-11-21)

- last only
"""


def test_heading_with_date():
    assert release_notes(CHANGELOG, "1.1.0") == "- dated only\n- dated second"


def test_heading_without_date():
    assert release_notes(CHANGELOG, "1.2.0") == "- undated only\n- undated second\n\n### Kept\n\n- subsection stays"


def test_last_section():
    assert release_notes(CHANGELOG, "1.0.0") == "- last only"


def test_missing_section():
    assert release_notes(CHANGELOG, "9.9.9") == "See CHANGELOG.md."


def test_does_not_leak_the_next_section():
    notes = release_notes(CHANGELOG, "1.1.0")
    assert "## " not in notes
    assert "ten only" not in notes
    assert "last only" not in notes
    assert "unreleased only" not in notes
    assert release_notes(CHANGELOG, "1.10.0") == "- ten only"
    assert "dated only" not in release_notes(CHANGELOG, "1.10.0")


def test_empty_section_points_at_changelog():
    text = "## 2.0.0\n\n\n## 2.0.1\n\n- next only\n"
    assert release_notes(text, "2.0.0") == "See CHANGELOG.md."
    assert "next only" not in release_notes(text, "2.0.0")


def test_crlf_section_stops_before_the_next_heading():
    text = "## 1.0.0 (2026-11-21)\r\n\r\n- line\r\n\r\n## 1.0.1\r\n\r\n- next\r\n"
    assert release_notes(text, "1.0.0") == "- line"


def test_cli_prints_utf8_notes(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text("## 1.0.0\n\n- 日本語\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, ROOT_SCRIPT, "--version", "1.0.0", "--changelog", str(path)],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout == "- 日本語\n"


def test_highest_semver_orders_numeric_components_and_suffixes():
    assert highest_semver(["v1.0.0", "v1.0.1", "v1.1.0"]) == "v1.1.0"
    assert highest_semver(["v1.9.0", "v1.10.0"]) == "v1.10.0"
    assert highest_semver(["v1.2.0-rc.1", "v1.2.0", "v1.1.0"]) == "v1.2.0"
    assert highest_semver(["v1.2.0-rc.1", "v1.2.0-rc.2"]) == "v1.2.0-rc.2"
    assert highest_semver(["v1.2.0-alpha", "v1.2.0-rc.1"]) == "v1.2.0-rc.1"
    assert highest_semver(["v1.0.0.1", "v1.0.0"]) == "v1.0.0"
    assert highest_semver(["vlatest", "v1.0.0"]) == "v1.0.0"
    assert highest_semver(["vlatest"]) is None


def test_highest_semver_cli_prints_the_tag():
    result = subprocess.run(
        [sys.executable, ROOT_SCRIPT, "--highest", "v1.0.0", "v1.10.0", "v1.9.0", "vlatest"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout == "v1.10.0\n"
    empty = subprocess.run(
        [sys.executable, ROOT_SCRIPT, "--highest", "vlatest"],
        capture_output=True, text=True, check=True,
    )
    assert empty.stdout == ""
