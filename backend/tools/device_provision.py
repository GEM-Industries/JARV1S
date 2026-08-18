#!/usr/bin/env python
"""CLI provisioning for per-device WebSocket credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import quote

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "JARV1S"
APP_SOCKET = APP_SUPPORT / "run" / "mongodb-0.sock"


def _configure_app() -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.25)
            client.connect(str(APP_SOCKET))
    except OSError as exc:
        raise SystemExit(f"JARV1S is not running; app socket is unavailable: {APP_SOCKET}") from exc
    os.environ["JARVIS_DATA_DIR"] = str(APP_SUPPORT)
    os.environ["MONGODB_URL"] = f"mongodb://{quote(str(APP_SOCKET), safe='')}"


async def _connect():
    _configure_app()
    from services.database.mongodb import mongodb

    await mongodb.connect()
    return mongodb


async def _disconnect(mongodb) -> None:
    await mongodb.disconnect()


async def cmd_pair_code(args: argparse.Namespace) -> int:
    mongodb = await _connect()
    try:
        from core.auth.device_service import device_auth_service

        result = await device_auth_service.issue_pairing_code(
            owner_id=args.owner_id,
            node_label=args.node_label,
        )
    finally:
        await _disconnect(mongodb)
    print(f"Pairing code: {result.code}")
    print(f"Owner: {result.owner_id}")
    print(f"Expires: {result.expires_at.isoformat()}")
    return 0


async def cmd_satellite_token(args: argparse.Namespace) -> int:
    mongodb = await _connect()
    try:
        from core.auth.device_models import DeviceLocation
        from core.auth.device_service import device_auth_service

        location = None
        if any((args.room_id, args.room_name, args.ha_area_id)):
            location = DeviceLocation(
                provider="manual" if (args.room_id or args.room_name) else "home_assistant",
                room_id=args.room_id,
                room_name=args.room_name,
                ha_area_id=args.ha_area_id,
            )
        summary, token = await device_auth_service.create_satellite_credential(
            owner_id=args.owner_id,
            node_id=args.node_id,
            node_label=args.node_label,
            capabilities=[part.strip() for part in args.capabilities.split(",") if part.strip()],
            location=location,
        )
    finally:
        await _disconnect(mongodb)
    print(f"Device ID: {summary.device_id}")
    print(f"Owner: {summary.owner_id}")
    print(f"Node: {summary.node_id}")
    print(f"Device token: {token}")
    return 0


async def cmd_revoke(args: argparse.Namespace) -> int:
    mongodb = await _connect()
    try:
        from core.auth.device_service import device_auth_service

        revoked = await device_auth_service.revoke_device(args.device_id)
    finally:
        await _disconnect(mongodb)
    if not revoked:
        print(f"Device not found or already revoked: {args.device_id}", file=sys.stderr)
        return 1
    print(f"Revoked device: {args.device_id}")
    return 0


async def cmd_list(args: argparse.Namespace) -> int:
    mongodb = await _connect()
    try:
        from core.auth.device_service import device_auth_service

        devices = await device_auth_service.list_devices(owner_id=args.owner_id)
    finally:
        await _disconnect(mongodb)
    print(json.dumps([device.model_dump(mode="json") for device in devices], indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provision per-device WebSocket credentials.")
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair-code", help="Issue a browser/phone pairing code (operator only).")
    pair.add_argument("--owner-id")
    pair.add_argument("--node-label")

    sat = sub.add_parser("satellite-token", help="Create a satellite device token.")
    sat.add_argument("--node-id", required=True)
    sat.add_argument("--owner-id")
    sat.add_argument("--node-label")
    sat.add_argument("--capabilities", default="mic,speaker")
    sat.add_argument("--room-id")
    sat.add_argument("--room-name")
    sat.add_argument("--ha-area-id")

    revoke = sub.add_parser("revoke", help="Revoke a device credential.")
    revoke.add_argument("device_id")

    listing = sub.add_parser("list", help="List provisioned devices.")
    listing.add_argument("--owner-id")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    handlers = {
        "pair-code": cmd_pair_code,
        "satellite-token": cmd_satellite_token,
        "revoke": cmd_revoke,
        "list": cmd_list,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
