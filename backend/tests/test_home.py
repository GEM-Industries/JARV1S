from __future__ import annotations

from pathlib import Path

import pytest

from core import home as home_mod
from core.home import load_home_snapshot, seed_home


@pytest.fixture
def home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(home_mod.settings, "DATA_DIR", tmp_path)
    return tmp_path / "home"


def _write_skill(root: Path, dirname: str, body: str) -> None:
    skill_dir = root / "skills" / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def test_missing_files_are_harmless(home_dir: Path) -> None:
    snapshot = load_home_snapshot()
    assert snapshot.prompt is None
    assert snapshot.skills == ()
    assert snapshot.issues == ()


def test_edits_appear_on_next_load(home_dir: Path) -> None:
    seed_home()
    prompt_path = home_dir / "PROMPT.md"
    seeded_prompt = prompt_path.read_text(encoding="utf-8")
    assert "long-standing confidant" in seeded_prompt
    assert "Address me as “sir” sparingly" in seeded_prompt
    assert "When I am venting or struggling, turn the wit off" in seeded_prompt
    assert "These examples demonstrate range and restraint" in seeded_prompt
    assert not (home_dir / "IDENTITY.md").exists()
    assert not (home_dir / "INSTRUCTIONS.md").exists()
    prompt_path.write_text("Call me Geoff.", encoding="utf-8")
    assert load_home_snapshot().prompt == "Call me Geoff."
    prompt_path.write_text("Call me G.", encoding="utf-8")
    seed_home()
    assert load_home_snapshot().prompt == "Call me G."


def test_valid_skill_metadata_is_discovered(home_dir: Path) -> None:
    _write_skill(
        home_dir,
        "briefing",
        "---\nname: briefing\ndescription: Morning briefing.\ncompatibility: voice\n---\n"
        "Do not inject this body.\n",
    )
    snapshot = load_home_snapshot()
    assert len(snapshot.skills) == 1
    skill = snapshot.skills[0]
    assert skill.name == "briefing"
    assert skill.description == "Morning briefing."
    assert skill.compatibility == "voice"
    assert skill.path == (home_dir / "skills" / "briefing" / "SKILL.md").resolve()
    assert snapshot.issues == ()


def test_unsafe_entries_are_excluded(home_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("leak", encoding="utf-8")
    home_dir.mkdir()
    (home_dir / "PROMPT.md").symlink_to(outside)
    _write_skill(home_dir, "bad-name", "---\nname: Not Valid\ndescription: nope\n---\n")
    _write_skill(home_dir, "briefing", "---\nname: briefing\ndescription: keep me\n---\n")
    _write_skill(home_dir, "briefing-dup", "---\nname: briefing\ndescription: duplicate\n---\n")

    snapshot = load_home_snapshot()
    assert snapshot.prompt is None
    assert [skill.name for skill in snapshot.skills] == ["briefing"]
    reasons = {issue.reason for issue in snapshot.issues}
    assert "symlink escape" in reasons
    assert "invalid skill name" in reasons
    assert any("duplicate skill name" in issue.reason for issue in snapshot.issues)
