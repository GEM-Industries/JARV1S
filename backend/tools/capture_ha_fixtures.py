"""Capture Home Assistant API response shapes for unit-test fixtures."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import tempfile
from pathlib import Path
from typing import Any

from plugins.smart_home.bootstrap_config import (
    BOOTSTRAP_HA_URL,
    FIXTURE_MANIFEST_PATH,
    HA_CONTAINER_IMAGE,
)
from plugins.smart_home.ha_client import HomeAssistantClient

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "ha"
REQUIRED_FIXTURES = [
    "api_ping.json",
    "auth_token_response.json",
    "current_user_response.json",
    "onboarding_users_response.json",
    "onboarding_status_pending.json",
    "onboarding_status_complete.json",
    "area_registry_list.json",
    "device_registry_list.json",
    "entity_registry_list.json",
    "states_sample.json",
]


def _write(fixtures_dir: Path, name: str, data: object) -> None:
    path = fixtures_dir / name
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}")


def _write_manifest(fixtures_dir: Path, client_id: str) -> None:
    manifest = {
        "ha_image": HA_CONTAINER_IMAGE,
        "bootstrap_url": BOOTSTRAP_HA_URL,
        "indieauth_client_id": client_id,
        "required_fixtures": REQUIRED_FIXTURES,
    }
    path = fixtures_dir / FIXTURE_MANIFEST_PATH.name
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}")


def _fixture_auth_token_response(refresh_token: str | None) -> dict[str, Any]:
    return {
        "access_token": "fixture-access-token",
        "token_type": "Bearer",
        "expires_in": 1800,
        "refresh_token": "fixture-refresh-token" if refresh_token else None,
    }


def _shape(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: _shape(data[key]) for key in sorted(data)}
    if isinstance(data, list):
        return [_shape(data[0])] if data else []
    if data is None:
        return "null"
    return type(data).__name__


def compare_fixture_shapes(actual_dir: Path, expected_dir: Path = FIXTURES_DIR) -> list[str]:
    manifest = json.loads((expected_dir / "manifest.json").read_text(encoding="utf-8"))
    diffs: list[str] = []
    for name in manifest["required_fixtures"]:
        actual_path = actual_dir / name
        expected_path = expected_dir / name
        if not actual_path.exists():
            diffs.append(f"{name}: missing from capture")
            continue
        actual = _shape(json.loads(actual_path.read_text(encoding="utf-8")))
        expected = _shape(json.loads(expected_path.read_text(encoding="utf-8")))
        if actual != expected:
            diffs.append(f"{name}: expected shape {expected!r}, got {actual!r}")
    return diffs


async def capture(
    url: str,
    token: str | None,
    onboarding: bool,
    *,
    fixtures_dir: Path = FIXTURES_DIR,
    name: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> None:
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    client = HomeAssistantClient(base_url=url, token=token)
    client_id = client.indieauth_client_id()
    try:
        if onboarding:
            pending = await client.onboarding_pending()
            print(f"Onboarding pending: {pending}")
            if pending:
                steps = await client.get_onboarding_steps()
                _write(fixtures_dir, "onboarding_status_pending.json", steps)

                owner_name = name or input("Owner display name [Fixture Owner]: ").strip() or "Fixture Owner"
                owner_username = username or input("Username [fixture]: ").strip() or "fixture"
                owner_password = password or getpass.getpass("Password: ").strip()
                if not owner_password:
                    raise SystemExit("Password required for onboarding capture")

                onboarding_result = await client.create_onboarding_user(
                    name=owner_name,
                    username=owner_username,
                    password=owner_password,
                )
                _write(fixtures_dir, "onboarding_users_response.json", {"auth_code": onboarding_result.auth_code})

                auth_result = await client.exchange_auth_code(
                    onboarding_result.auth_code,
                    client_id=client_id,
                )
                _write(
                    fixtures_dir,
                    "auth_token_response.json",
                    _fixture_auth_token_response(auth_result.refresh_token),
                )
                client.token = auth_result.access_token

                await client.complete_core_config()
                await client.complete_analytics(analytics_opt_in=False)
                await client.complete_integration(client_id=client_id)

                complete_steps = await client.get_onboarding_steps()
                _write(fixtures_dir, "onboarding_status_complete.json", complete_steps)

                token = await client.create_long_lived_access_token("JARV1S-fixture-capture")
                client.token = token
                print("Full bootstrap onboarding captured.")
            else:
                print("Onboarding already complete — skipping onboarding capture.")

        if token:
            client.token = token

        if client.token:
            _write(fixtures_dir, "api_ping.json", await client.ping())
            _write(fixtures_dir, "current_user_response.json", await client.current_user())
            _write(fixtures_dir, "states_sample.json", await client.get_states())
            _write(fixtures_dir, "area_registry_list.json", await client.list_areas())
            _write(fixtures_dir, "device_registry_list.json", await client.list_devices())
            _write(fixtures_dir, "entity_registry_list.json", await client.list_entities_registry())
            _write_manifest(fixtures_dir, client_id)
        else:
            print("No token — only unauthenticated endpoints captured.")
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture HA API fixtures against pinned bootstrap image")
    parser.add_argument("--url", default=BOOTSTRAP_HA_URL)
    parser.add_argument("--token", default=None, help="Existing long-lived token (skip onboarding)")
    parser.add_argument(
        "--onboarding",
        action="store_true",
        help="Capture full bootstrap onboarding flow (fresh HA instance)",
    )
    parser.add_argument("--name", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--output-dir", type=Path, default=FIXTURES_DIR)
    parser.add_argument(
        "--check-drift",
        action="store_true",
        help="Capture to a temp directory and fail if response shapes differ from committed fixtures",
    )
    args = parser.parse_args()

    if args.check_drift:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            asyncio.run(
                capture(
                    args.url,
                    args.token,
                    args.onboarding,
                    fixtures_dir=tmp_dir,
                    name=args.name,
                    username=args.username,
                    password=args.password,
                )
            )
            diffs = compare_fixture_shapes(tmp_dir)
        if diffs:
            print("Fixture drift detected:")
            for diff in diffs:
                print(f"  - {diff}")
            raise SystemExit(1)
        print("Fixture shapes match committed fixtures.")
        return

    asyncio.run(
        capture(
            args.url,
            args.token,
            args.onboarding,
            fixtures_dir=args.output_dir,
            name=args.name,
            username=args.username,
            password=args.password,
        )
    )


if __name__ == "__main__":
    main()
