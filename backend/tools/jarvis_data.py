"""Offline data maintenance for the installed (dogfood) JARV1S app.

Durable state backed up:
  mongo/             — LLM config, HA URL, OAuth, transcript, rooms, devices, …
  credentials/       — encrypted API keys, HA_TOKEN, provider secrets
  host-prefs.json    — launch / availability preferences
  voice/             — speaker enrollment profiles

Not backed up (large or ephemeral): models/, cache/, valkey/, run/, logs.

Usage (quit JARV1S first for backup / restore / reset):
    uv run python tools/jarvis_data.py backup              # → JARV1S Backups/daily
    uv run python tools/jarvis_data.py restore --yes         # ← daily
    uv run python tools/jarvis_data.py reset --yes           # first-run wipe
    uv run python tools/jarvis_data.py list
    uv run python tools/jarvis_data.py status
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from core.config import settings

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "JARV1S"
APP_SOCKET = APP_SUPPORT / "run" / "mongodb-0.sock"
APP_TCP_URL = "mongodb://127.0.0.1:27018"
APP_BACKUPS = APP_SUPPORT.parent / "JARV1S Backups"
MANIFEST_NAME = "manifest.json"
BACKUP_FORMAT_VERSION = 2
DURABLE_ENTRIES = ("mongo", "credentials", "host-prefs.json", "voice")
FACTORY_WIPE_ENTRIES = ("mongo", "credentials", "voice")
DEFAULT_BACKUP_NAME = "daily"
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
CREDENTIAL_PORTABILITY = {
    "requirement": "same macOS machine Keychain / secrets.enc passphrase",
    "cross_machine": "reauthentication may be required",
}


def _mongo_socket_url(socket_path: Path) -> str:
    return f"mongodb://{quote(str(socket_path), safe='')}"


def _probe_mongo(url: str) -> tuple[bool, Path | None]:
    client: MongoClient | None = None
    try:
        client = MongoClient(
            url,
            serverSelectionTimeoutMS=500,
            connectTimeoutMS=500,
        )
        client.admin.command("ping")
        options = client.admin.command("getCmdLineOpts")
        db_path = options.get("parsed", {}).get("storage", {}).get("dbPath")
        return True, Path(db_path) if isinstance(db_path, str) else None
    except (OSError, PyMongoError):
        return False, None
    finally:
        if client is not None:
            client.close()


def _same_path(left: Path | None, right: Path) -> bool:
    return left is not None and left.expanduser().resolve() == right.expanduser().resolve()


def _running_app_mongo_url() -> str | None:
    socket_url = _mongo_socket_url(APP_SOCKET)
    if APP_SOCKET.exists() and _probe_mongo(socket_url)[0]:
        return socket_url

    tcp_running, tcp_db_path = _probe_mongo(APP_TCP_URL)
    if tcp_running and _same_path(tcp_db_path, APP_SUPPORT / "mongo"):
        return APP_TCP_URL
    return None


def _app_running() -> bool:
    return _running_app_mongo_url() is not None


def _require_app_stopped() -> None:
    if _app_running():
        raise SystemExit("Quit JARV1S before changing or copying its data.")


def _require_app_data() -> None:
    if not (APP_SUPPORT / "mongo").is_dir():
        raise SystemExit(f"No installed app database found at {APP_SUPPORT / 'mongo'}")


def _validate_backup_name(name: str) -> str:
    if not NAME_PATTERN.fullmatch(name):
        raise SystemExit(
            "Backup name must be 1–64 chars: letters, digits, '.', '_', '-' "
            "(start with a letter or digit)."
        )
    if name in {".", ".."} or name.startswith("."):
        raise SystemExit("Backup name cannot be a hidden or relative path segment.")
    return name


def _resolve_backup_source(source: Path) -> Path:
    """Accept an absolute/relative path, or a named slot under JARV1S Backups."""
    expanded = source.expanduser()
    if expanded.exists():
        return expanded.resolve()
    if len(expanded.parts) == 1:
        named = APP_BACKUPS / expanded.name
        if named.exists():
            return named.resolve()
    return expanded.resolve()


def _entry_bytes(root: Path, name: str) -> int:
    path = root / name
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.2f} GB"


async def cmd_status() -> int:
    mongo_url = _running_app_mongo_url()
    if mongo_url is None:
        raise SystemExit("JARV1S packaged MongoDB is not running.")

    client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=3000)
    try:
        await client.admin.command("ping")
        db = client[settings.DATABASE_NAME]
        print(f"[app] {mongo_url}")
        print(f"[data] {APP_SUPPORT}")
        names = await db.list_collection_names()
        if not names:
            print("  reachable (empty database)")
        for name in sorted(names):
            print(f"  {name}: {await db[name].count_documents({})}")
        creds = APP_SUPPORT / "credentials" / "secrets.enc"
        print(f"  credentials: {'present' if creds.is_file() else 'missing'}")
    finally:
        client.close()
    return 0


def cmd_list() -> int:
    print(f"[backups] {APP_BACKUPS}")
    if not APP_BACKUPS.is_dir():
        print("  (none yet — quit JARV1S and run: task desktop:data:backup)")
        return 0

    slots = sorted(
        path for path in APP_BACKUPS.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / MANIFEST_NAME).is_file()
    )
    if not slots:
        print("  (none yet)")
        return 0

    for path in slots:
        try:
            manifest = _load_manifest(path)
            created = manifest.get("created_at", "?")
            entries = ", ".join(manifest.get("entries", []))
            total = sum(_entry_bytes(path, name) for name in manifest.get("entries", []))
            print(f"  {path.name}")
            print(f"    created: {created}")
            print(f"    size:    {_format_bytes(total)}")
            print(f"    entries: {entries}")
            print(f"    path:    {path}")
        except SystemExit as exc:
            print(f"  {path.name}: invalid ({exc})")
    return 0


def _copy_entry(source_root: Path, destination_root: Path, name: str) -> bool:
    source = source_root / name
    if not source.exists():
        return False
    destination = destination_root / name
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_records(root: Path, entries: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entry in entries:
        path = root / entry
        files = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
        for file_path in files:
            records.append(
                {
                    "path": file_path.relative_to(root).as_posix(),
                    "size": file_path.stat().st_size,
                    "sha256": _sha256(file_path),
                }
            )
    return records


def _validate_file_records(entries: list[str], records: object) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise SystemExit("Backup manifest does not contain file checksums.")

    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise SystemExit("Backup manifest contains an invalid file record.")
        path = record.get("path")
        size = record.get("size")
        checksum = record.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(checksum, str)
            or len(checksum) != 64
        ):
            raise SystemExit("Backup manifest contains an invalid file record.")
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise SystemExit("Backup manifest contains an unsafe file path.")
        if relative.parts[0] not in entries or path in seen:
            raise SystemExit("Backup manifest contains an unsupported file path.")
        seen.add(path)
        validated.append({"path": path, "size": size, "sha256": checksum})
    return validated


def _verify_backup(root: Path, manifest: dict[str, Any]) -> None:
    entries = manifest["entries"]
    expected_records = _validate_file_records(entries, manifest.get("files"))
    expected = {record["path"]: record for record in expected_records}
    actual_records = _file_records(root, entries)
    actual = {record["path"]: record for record in actual_records}

    all_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if all_files != set(expected):
        raise SystemExit("Backup files do not match the manifest.")
    for path, record in expected.items():
        if actual.get(path) != record:
            raise SystemExit(f"Backup integrity check failed: {path}")


def _load_manifest(source: Path) -> dict:
    manifest_path = source / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SystemExit(f"Missing backup manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid backup manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise SystemExit("Unsupported JARV1S backup format.")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not all(isinstance(item, str) for item in entries):
        raise SystemExit("Backup manifest entries are invalid.")
    if any(item not in DURABLE_ENTRIES for item in entries):
        raise SystemExit("Backup manifest contains an unsupported path.")
    _verify_backup(source, manifest)
    return manifest


def _write_backup_tree(destination: Path) -> list[str]:
    stage = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    entries: list[str] = []
    try:
        stage.mkdir(parents=True)
        for name in DURABLE_ENTRIES:
            if _copy_entry(APP_SUPPORT, stage, name):
                entries.append(name)
        if not entries:
            raise SystemExit("Nothing durable to back up under the app data directory.")
        if "credentials" not in entries:
            print("warning: credentials/ missing — API keys and HA_TOKEN will not be in this backup")
        if "mongo" not in entries:
            print("warning: mongo/ missing — LLM/HA/OAuth config will not be in this backup")
        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
            "credential_portability": CREDENTIAL_PORTABILITY,
            "files": _file_records(stage, entries),
        }
        (stage / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _verify_backup(stage, manifest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            previous = destination.parent / f".{destination.name}.previous-{uuid.uuid4().hex}"
            destination.rename(previous)
            try:
                stage.rename(destination)
            except BaseException:
                previous.rename(destination)
                raise
            shutil.rmtree(previous, ignore_errors=True)
        else:
            stage.rename(destination)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return entries


def cmd_backup(out: Path | None, name: str | None = None) -> int:
    _require_app_stopped()
    _require_app_data()
    if out is not None and name is not None:
        raise SystemExit("Pass either --name or --out, not both.")

    if out is not None:
        destination = out.expanduser()
        if destination.exists():
            raise SystemExit(f"Backup destination already exists: {destination}")
    else:
        slot = _validate_backup_name(name or DEFAULT_BACKUP_NAME)
        destination = APP_BACKUPS / slot

    entries = _write_backup_tree(destination)
    total = sum(_entry_bytes(destination, entry) for entry in entries)
    print(f"Dogfood backup saved: {destination}")
    print(f"  size:    {_format_bytes(total)}")
    print(f"  entries: {', '.join(entries)}")
    print("  includes: Mongo config + encrypted keys + host prefs + voice profiles")
    print("  excludes: models/, cache/, valkey/, run/ (ephemeral or re-downloadable)")
    return 0


def cmd_restore(source: Path | None, *, yes: bool) -> int:
    _require_app_stopped()
    if not yes:
        raise SystemExit("Pass --yes to replace the installed app data.")
    resolved = _resolve_backup_source(source or Path(DEFAULT_BACKUP_NAME))
    manifest = _load_manifest(resolved)

    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    parent = APP_SUPPORT.parent
    stage = parent / f".JARV1S.restore-{uuid.uuid4().hex}"
    previous = parent / f".JARV1S.previous-{uuid.uuid4().hex}"
    try:
        stage.mkdir(parents=True)
        for name in manifest["entries"]:
            _copy_entry(resolved, stage, name)
        _verify_backup(stage, manifest)

        previous.mkdir(parents=True)
        # Swap only durable entries — keep models/, cache/, valkey/, run/ in place.
        for name in DURABLE_ENTRIES:
            live = APP_SUPPORT / name
            staged = stage / name
            if live.exists():
                live.rename(previous / name)
            if staged.exists():
                staged.rename(live)

        shutil.rmtree(previous, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)
    except BaseException:
        # Best-effort rollback of any entries already moved into previous.
        if previous.exists():
            for name in DURABLE_ENTRIES:
                saved = previous / name
                live = APP_SUPPORT / name
                if saved.exists() and not live.exists():
                    saved.rename(live)
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)
        raise

    print(f"App data restored from: {resolved}")
    print(f"  entries: {', '.join(manifest['entries'])}")
    print("  preserved locally: models/, cache/, valkey/, run/")
    return 0


def cmd_reset(*, yes: bool) -> int:
    """First-run wipe: mongo + credentials + voice. Keeps host-prefs and models."""
    _require_app_stopped()
    if not yes:
        raise SystemExit("Pass --yes to confirm first-run reset.")

    removed: list[str] = []
    for name in FACTORY_WIPE_ENTRIES:
        path = APP_SUPPORT / name
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(name)
    print("First-run reset complete — SetupWizard will show on next launch.")
    print(f"  removed: {', '.join(removed) or '(nothing present)'}")
    print("  preserved: host-prefs.json, models/, cache/")
    print(f"  restore with: task desktop:data:restore   # defaults to '{DEFAULT_BACKUP_NAME}'")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Installed (dogfood) JARV1S app data maintenance",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Report app database reachability and collection counts")
    sub.add_parser("list", help="List backups under JARV1S Backups")
    backup = sub.add_parser(
        "backup",
        help=f"Back up durable app data (default slot: {DEFAULT_BACKUP_NAME})",
    )
    backup.add_argument("--out", type=Path, help="One-off directory (must not exist)")
    backup.add_argument(
        "--name",
        type=str,
        help=f"Named slot under JARV1S Backups (default: {DEFAULT_BACKUP_NAME})",
    )
    restore = sub.add_parser(
        "restore",
        help=f"Restore durable app data (default slot: {DEFAULT_BACKUP_NAME})",
    )
    restore.add_argument(
        "--source",
        type=Path,
        default=None,
        help=f"Backup path or slot name (default: {DEFAULT_BACKUP_NAME})",
    )
    restore.add_argument("--yes", action="store_true", help="Confirm destructive restore")
    reset = sub.add_parser(
        "reset",
        help="First-run wipe (mongo + credentials + voice); keeps host-prefs/models",
    )
    reset.add_argument("--yes", action="store_true", help="Confirm destructive reset")
    return parser


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "status":
        return await cmd_status()
    if args.command == "list":
        return cmd_list()
    if args.command == "backup":
        return cmd_backup(args.out, name=args.name)
    if args.command == "restore":
        return cmd_restore(args.source, yes=args.yes)
    if args.command == "reset":
        return cmd_reset(yes=args.yes)
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
