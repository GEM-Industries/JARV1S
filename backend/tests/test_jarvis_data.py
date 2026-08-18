from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import jarvis_data


@pytest.fixture
def app_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    app_support = tmp_path / "Application Support" / "JARV1S"
    backups = app_support.parent / "JARV1S Backups"
    monkeypatch.setattr(jarvis_data, "APP_SUPPORT", app_support)
    monkeypatch.setattr(jarvis_data, "APP_SOCKET", app_support / "run" / "mongodb-0.sock")
    monkeypatch.setattr(jarvis_data, "APP_BACKUPS", backups)
    monkeypatch.setattr(jarvis_data, "_running_app_mongo_url", lambda: None)
    return app_support, backups


def _write_app_data(app_support: Path, value: str = "current") -> None:
    (app_support / "mongo").mkdir(parents=True)
    (app_support / "mongo" / "data.wt").write_text(value)
    (app_support / "credentials").mkdir()
    (app_support / "credentials" / "secrets.enc").write_text("encrypted")
    (app_support / "host-prefs.json").write_text('{"external_triggers_enabled": true}')
    (app_support / "voice" / "speaker-profiles").mkdir(parents=True)
    (app_support / "voice" / "speaker-profiles" / "owner.npz").write_bytes(b"profile")
    (app_support / "run").mkdir()
    (app_support / "run" / "stale.sock").write_text("")


@pytest.mark.parametrize(
    "mongo_url",
    [
        "mongodb://%2Ftmp%2Fmongodb-0.sock",
        jarvis_data.APP_TCP_URL,
    ],
)
def test_backup_refuses_while_packaged_mongo_is_running(
    app_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    mongo_url: str,
) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    monkeypatch.setattr(jarvis_data, "_running_app_mongo_url", lambda: mongo_url)

    with pytest.raises(SystemExit, match="Quit JARV1S"):
        jarvis_data.cmd_backup(None)


def test_running_app_mongo_detects_packaged_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_support = tmp_path / "Application Support" / "JARV1S"
    app_socket = app_support / "run" / "mongodb-0.sock"
    app_socket.parent.mkdir(parents=True)
    app_socket.touch()
    socket_url = jarvis_data._mongo_socket_url(app_socket)
    monkeypatch.setattr(jarvis_data, "APP_SUPPORT", app_support)
    monkeypatch.setattr(jarvis_data, "APP_SOCKET", app_socket)
    monkeypatch.setattr(
        jarvis_data,
        "_probe_mongo",
        lambda url: (True, app_support / "mongo") if url == socket_url else (False, None),
    )

    assert jarvis_data._running_app_mongo_url() == socket_url


def test_running_app_mongo_detects_packaged_tcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_support = tmp_path / "Application Support" / "JARV1S"
    monkeypatch.setattr(jarvis_data, "APP_SUPPORT", app_support)
    monkeypatch.setattr(jarvis_data, "APP_SOCKET", app_support / "run" / "mongodb-0.sock")
    monkeypatch.setattr(
        jarvis_data,
        "_probe_mongo",
        lambda url: (True, app_support / "mongo")
        if url == jarvis_data.APP_TCP_URL
        else (False, None),
    )

    assert jarvis_data._running_app_mongo_url() == jarvis_data.APP_TCP_URL


def test_running_app_mongo_ignores_contributor_tcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_support = tmp_path / "Application Support" / "JARV1S"
    monkeypatch.setattr(jarvis_data, "APP_SUPPORT", app_support)
    monkeypatch.setattr(jarvis_data, "APP_SOCKET", app_support / "run" / "mongodb-0.sock")
    monkeypatch.setattr(jarvis_data, "_probe_mongo", lambda _url: (True, tmp_path / "dev-mongo"))

    assert jarvis_data._running_app_mongo_url() is None


def test_backup_copies_only_durable_app_state(app_paths: tuple[Path, Path]) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    destination = app_support.parent / "backup"

    assert jarvis_data.cmd_backup(destination) == 0

    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["format_version"] == jarvis_data.BACKUP_FORMAT_VERSION
    assert manifest["entries"] == ["mongo", "credentials", "host-prefs.json", "voice"]
    assert manifest["credential_portability"] == jarvis_data.CREDENTIAL_PORTABILITY
    assert [record["path"] for record in manifest["files"]] == [
        "mongo/data.wt",
        "credentials/secrets.enc",
        "host-prefs.json",
        "voice/speaker-profiles/owner.npz",
    ]
    assert (destination / "mongo" / "data.wt").read_text() == "current"
    assert (destination / "credentials" / "secrets.enc").read_text() == "encrypted"
    assert (destination / "voice" / "speaker-profiles" / "owner.npz").read_bytes() == b"profile"
    assert not (destination / "run").exists()


def test_restore_replaces_app_data_from_valid_backup(app_paths: tuple[Path, Path]) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    (app_support / "models" / "ollama").mkdir(parents=True)
    (app_support / "models" / "ollama" / "blob").write_text("large-model")
    source = app_support.parent / "backup"
    assert jarvis_data.cmd_backup(source) == 0
    (app_support / "mongo" / "data.wt").write_text("changed after backup")

    assert jarvis_data.cmd_restore(source, yes=True) == 0

    assert (app_support / "mongo" / "data.wt").read_text() == "current"
    assert (app_support / "credentials" / "secrets.enc").read_text() == "encrypted"
    assert (app_support / "voice" / "speaker-profiles" / "owner.npz").exists()
    assert (app_support / "models" / "ollama" / "blob").read_text() == "large-model"
    assert (app_support / "run" / "stale.sock").exists()


def test_backup_defaults_to_daily_slot(app_paths: tuple[Path, Path]) -> None:
    app_support, backups = app_paths
    _write_app_data(app_support)

    assert jarvis_data.cmd_backup(None) == 0
    assert (backups / "daily" / "mongo" / "data.wt").read_text() == "current"


def test_named_backup_replaces_stable_slot(app_paths: tuple[Path, Path]) -> None:
    app_support, backups = app_paths
    _write_app_data(app_support)

    assert jarvis_data.cmd_backup(None, name="daily") == 0
    assert (backups / "daily" / "mongo" / "data.wt").read_text() == "current"
    (app_support / "mongo" / "data.wt").write_text("next")
    assert jarvis_data.cmd_backup(None, name="daily") == 0
    assert (backups / "daily" / "mongo" / "data.wt").read_text() == "next"
    assert list(backups.glob(".daily.previous-*")) == []


def test_restore_defaults_to_daily_slot(app_paths: tuple[Path, Path]) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    assert jarvis_data.cmd_backup(None) == 0
    (app_support / "mongo" / "data.wt").write_text("changed")

    assert jarvis_data.cmd_restore(None, yes=True) == 0
    assert (app_support / "mongo" / "data.wt").read_text() == "current"


def test_restore_accepts_backup_slot_name(app_paths: tuple[Path, Path]) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    assert jarvis_data.cmd_backup(None, name="pre-ftue") == 0
    (app_support / "mongo" / "data.wt").write_text("changed")

    assert jarvis_data.cmd_restore(Path("pre-ftue"), yes=True) == 0
    assert (app_support / "mongo" / "data.wt").read_text() == "current"


def test_restore_requires_confirmation_without_changing_data(
    app_paths: tuple[Path, Path],
) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    source = app_support.parent / "backup"
    assert jarvis_data.cmd_backup(source) == 0

    with pytest.raises(SystemExit, match="--yes"):
        jarvis_data.cmd_restore(source, yes=False)

    assert (app_support / "mongo" / "data.wt").read_text() == "current"


@pytest.mark.parametrize("corruption", ["modified", "missing", "unexpected"])
def test_restore_rejects_corrupt_backup_without_changing_app_data(
    app_paths: tuple[Path, Path],
    corruption: str,
) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    source = app_support.parent / "backup"
    assert jarvis_data.cmd_backup(source) == 0
    (app_support / "mongo" / "data.wt").write_text("live")

    if corruption == "modified":
        (source / "mongo" / "data.wt").write_text("tampered")
    elif corruption == "missing":
        (source / "mongo" / "data.wt").unlink()
    else:
        (source / "mongo" / "unexpected.wt").write_text("unexpected")

    with pytest.raises(SystemExit, match="Backup"):
        jarvis_data.cmd_restore(source, yes=True)

    assert (app_support / "mongo" / "data.wt").read_text() == "live"
    assert not list(app_support.parent.glob(".JARV1S.restore-*"))
    assert not list(app_support.parent.glob(".JARV1S.previous-*"))


def test_restore_verifies_staged_copy_before_swap(
    app_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    source = app_support.parent / "backup"
    assert jarvis_data.cmd_backup(source) == 0
    (app_support / "mongo" / "data.wt").write_text("live")
    copy_entry = jarvis_data._copy_entry

    def copy_then_corrupt(source_root: Path, destination_root: Path, name: str) -> bool:
        copied = copy_entry(source_root, destination_root, name)
        if destination_root.name.startswith(".JARV1S.restore-") and name == "mongo":
            (destination_root / "mongo" / "data.wt").write_text("corrupt staged copy")
        return copied

    monkeypatch.setattr(jarvis_data, "_copy_entry", copy_then_corrupt)

    with pytest.raises(SystemExit, match="integrity check failed"):
        jarvis_data.cmd_restore(source, yes=True)

    assert (app_support / "mongo" / "data.wt").read_text() == "live"
    assert not list(app_support.parent.glob(".JARV1S.restore-*"))
    assert not list(app_support.parent.glob(".JARV1S.previous-*"))


def test_restore_rejects_unsupported_manifest_entry(app_paths: tuple[Path, Path]) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    source = app_support.parent / "backup"
    source.mkdir()
    (source / "mongo").mkdir()
    (source / "unexpected").mkdir()
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "format_version": jarvis_data.BACKUP_FORMAT_VERSION,
                "entries": ["mongo", "unexpected"],
            }
        )
    )

    with pytest.raises(SystemExit, match="unsupported path"):
        jarvis_data.cmd_restore(source, yes=True)


def test_reset_is_first_run_wipe(app_paths: tuple[Path, Path]) -> None:
    app_support, _ = app_paths
    _write_app_data(app_support)
    (app_support / "models" / "ollama").mkdir(parents=True)
    (app_support / "models" / "ollama" / "blob").write_text("keep")

    assert jarvis_data.cmd_reset(yes=True) == 0

    assert not (app_support / "mongo").exists()
    assert not (app_support / "credentials").exists()
    assert not (app_support / "voice").exists()
    assert (app_support / "host-prefs.json").exists()
    assert (app_support / "models" / "ollama" / "blob").read_text() == "keep"


def test_cli_has_no_split_target_or_transfer_commands() -> None:
    parser = jarvis_data.build_parser()
    help_text = parser.format_help()
    assert "--target" not in help_text
    assert "--factory" not in help_text
    for removed in ("export", "import"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed])
    args = parser.parse_args(["backup", "--name", "daily"])
    assert args.name == "daily"
    args = parser.parse_args(["reset", "--yes"])
    assert args.yes is True
    args = parser.parse_args(["restore", "--yes"])
    assert args.source is None
