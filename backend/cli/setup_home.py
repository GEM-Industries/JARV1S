"""
Home Assistant Setup Assistant.

Paths:
  1) Connect to existing Home Assistant (detect + paste token)
  2) Onboard a fresh HA instance you already have running
  3) Bootstrap HA Container locally (Docker required)

Run via: task setup:home
"""

from __future__ import annotations

import asyncio
import argparse
import getpass
import sys

from cli.setup_home_env import read_env_value, upsert_env
from plugins.smart_home.bootstrap import bootstrap_supported, run_bootstrap
from plugins.smart_home.config import persist_ha_connection
from plugins.smart_home.discovery import discover_home_assistant
from plugins.smart_home.ha_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
    HomeAssistantOnboardingError,
    normalize_ha_url,
)
from plugins.smart_home.status import check_liveness, check_readiness


async def _detect_ha_url() -> str | None:
    preferred = read_env_value("HA_URL")
    try:
        from services.database.mongodb import mongodb

        await mongodb.connect()
    except Exception:
        pass
    return await discover_home_assistant(preferred_url=preferred)


async def _validate_and_report(url: str, token: str) -> bool:
    liveness = await check_liveness(url, token)
    print(f"\n{liveness.message}")
    if not liveness.authenticated:
        return False

    client = HomeAssistantClient(base_url=url, token=token)
    try:
        readiness = await check_readiness(client)
        print(readiness.message)
        print(
            f"  Entities: {readiness.entity_count}, "
            f"safe controllable: {readiness.safe_controllable_count}"
        )
        if readiness.setup_candidate:
            print(f"  Suggested first device: {readiness.setup_candidate}")
        elif readiness.entity_count == 0:
            print("  No devices paired yet — add a device in Home Assistant next.")
    finally:
        await client.aclose()
    return True


async def _persist_connection(url: str, token: str) -> None:
    try:
        from services.database.mongodb import mongodb

        await mongodb.connect()
        await persist_ha_connection(url, token)
    except Exception as exc:
        print(f"Warning: could not save to product config store ({exc}).")
        from core.credentials.store import credential_store

        credential_store.set_secret("HA_TOKEN", token)
        upsert_env("HA_URL", url)
        return
    upsert_env("HA_URL", url)


async def _setup_existing(url: str) -> None:
    print(f"\nUsing Home Assistant at {url}")
    print("\nCreate a long-lived access token in Home Assistant:")
    print("  Profile → Security → Long-Lived Access Tokens → Create Token")
    token = getpass.getpass("\nPaste your HA access token: ").strip()
    if not token:
        print("No token provided — aborting.")
        sys.exit(1)

    if not await _validate_and_report(url, token):
        sys.exit(1)

    await _persist_connection(url, token)


async def _setup_fresh(url: str) -> None:
    client = HomeAssistantClient(base_url=url)
    try:
        pending = await client.onboarding_pending()
        if not pending:
            print("Home Assistant onboarding appears complete — use the existing-instance path.")
            await _setup_existing(url)
            return

        print("\nFresh Home Assistant onboarding detected.")
        name = input("Owner display name [JARV1S Owner]: ").strip() or "JARV1S Owner"
        username = input("Username [jarvis]: ").strip() or "jarvis"
        password = getpass.getpass("Password: ").strip()
        if not password:
            print("Password is required for fresh onboarding.")
            sys.exit(1)

        print("Completing onboarding steps...")
        long_lived = await client.complete_bootstrap_onboarding(
            owner_name=name,
            username=username,
            password=password,
        )
    except (HomeAssistantConnectionError, HomeAssistantOnboardingError, HomeAssistantAuthError) as e:
        print(f"\nOnboarding failed: {e}")
        print("Complete setup in the Home Assistant UI, then rerun and choose existing instance.")
        sys.exit(1)
    finally:
        await client.aclose()

    if not await _validate_and_report(url, long_lived):
        sys.exit(1)

    await _persist_connection(url, long_lived)


async def _setup_bootstrap() -> None:
    ok, reason = bootstrap_supported()
    if not ok:
        print(f"\n{reason}")
        print("\nAlternatives:")
        print("  - Install/start Docker Desktop, then choose option 2 again")
        print("  - Connect to an existing Home Assistant instance (option 1)")
        print("  - Use dedicated Home Assistant hardware (option 3)")
        sys.exit(1)

    print("\nJARV1S will download and start Home Assistant in Docker.")
    print("This takes a few minutes on first run.")
    confirm = input("Continue? [Y/n]: ").strip().lower()
    if confirm in {"n", "no"}:
        sys.exit(0)

    password = getpass.getpass("Choose a Home Assistant owner password: ").strip()
    if not password:
        print("Password is required.")
        sys.exit(1)

    try:
        result = await run_bootstrap(password=password)
    except Exception as e:
        print(f"\nBootstrap failed: {e}")
        sys.exit(1)

    if not await _validate_and_report(result.base_url, result.long_lived_token):
        sys.exit(1)

    await _persist_connection(result.base_url, result.long_lived_token)


def _print_hardware_guide() -> None:
    print("""
For a dedicated always-on hub without running Docker on your Mac, use Home Assistant hardware:

  - Home Assistant Green (easiest): plug in, open http://homeassistant.local:8123
  - Home Assistant on a Raspberry Pi / mini PC with Home Assistant OS

After HA is running, rerun this wizard and choose option 1 to connect JARV1S.
""")


async def async_main(*, bootstrap_only: bool = False) -> None:
    print("─" * 60)
    print("  Home Assistant Setup Assistant")
    print("─" * 60)

    if bootstrap_only:
        await _setup_bootstrap()
        print("\n" + "─" * 60)
        print("Home Assistant is running and connected.")
        print("Next: add devices in Smart Life, link Tuya in Home Assistant, then ask JARV1S to refresh.")
        print("─" * 60)
        return

    detected = await _detect_ha_url()
    if detected:
        print(f"\nDetected Home Assistant at {detected}")
    else:
        print("\nNo Home Assistant detected on the local network.")

    print("\nDo you already have Home Assistant?")
    print("  1) Yes — connect JARV1S to it")
    print("  2) No — install Home Assistant here with Docker")
    print("  3) No — I will use Home Assistant Green / HAOS hardware")
    choice = input("\nChoose [1]: ").strip() or "1"

    if choice == "2":
        await _setup_bootstrap()
    elif choice == "3":
        _print_hardware_guide()
        sys.exit(0)
    else:
        default_url = detected or read_env_value("HA_URL") or "http://homeassistant.local:8123"
        url_input = input(f"\nHome Assistant URL [{default_url}]: ").strip() or default_url
        try:
            url = normalize_ha_url(url_input)
        except ValueError as e:
            print(e)
            sys.exit(1)

        client = HomeAssistantClient(base_url=url)
        try:
            pending = await client.onboarding_pending()
        finally:
            await client.aclose()

        if pending:
            await _setup_fresh(url)
        else:
            await _setup_existing(url)

    print("\n" + "─" * 60)
    print("Home Assistant is connected.")
    print("Next: add devices in Smart Life, link Tuya in Home Assistant, then ask JARV1S to refresh.")
    print("─" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure the Home Assistant integration")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Provision Home Assistant Container via Docker and connect JARV1S",
    )
    args = parser.parse_args()
    asyncio.run(async_main(bootstrap_only=args.bootstrap))


if __name__ == "__main__":
    main()
