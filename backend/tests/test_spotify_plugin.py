import httpx
import pytest

from core.plugins.capabilities import CapabilityErrorDetail
from plugins.spotify import SpotifyPlugin
from plugins.spotify.client import ALLOWLIST_MESSAGE, NO_DEVICE_MESSAGE, SpotifyClientError, api_request


def _devices_payload(*rows: dict) -> dict:
    return {"devices": list(rows)}


@pytest.mark.asyncio
async def test_play_by_name_starts_on_resolved_device(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(client, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/me":
            return {"id": "geoff"}
        if path == "/me/player/devices":
            return _devices_payload(
                {"id": "mac", "name": "Geoff's MacBook", "is_active": True},
            )
        if path == "/me/playlists":
            return {
                "items": [
                    {
                        "name": "Focus Mix",
                        "uri": "spotify:playlist:abc",
                        "owner": {"id": "geoff"},
                    }
                ]
            }
        if path == "/me/player/play":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().play(name="Focus Mix", type="playlist", spotify=object())
    assert "Focus Mix" in result
    play = next(call for call in calls if call[1] == "/me/player/play")
    assert play[0] == "PUT"
    assert play[2]["params"] == {"device_id": "mac"}
    assert play[2]["json"] == {"context_uri": "spotify:playlist:abc"}


@pytest.mark.asyncio
async def test_play_without_devices_asks_to_open_spotify(monkeypatch):
    async def fake_request(client, method, path, **kwargs):
        if path == "/me/player/devices":
            return {"devices": []}
        if path == "/search":
            return {"tracks": {"items": [{"name": "Song", "uri": "spotify:track:1"}]}}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().play(name="Song", type="track", spotify=object())
    assert isinstance(result, CapabilityErrorDetail)
    assert result.message == NO_DEVICE_MESSAGE


@pytest.mark.asyncio
async def test_play_prefers_owned_library_playlist_over_followed_or_catalog(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(client, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/me":
            return {"id": "geoff"}
        if path == "/me/playlists":
            return {
                "items": [
                    {
                        "name": "Deep Sleep Mix",
                        "uri": "spotify:playlist:followed",
                        "owner": {"id": "other"},
                    },
                    {
                        "name": "sleep",
                        "uri": "spotify:playlist:mine",
                        "owner": {"id": "geoff"},
                    },
                ]
            }
        if path == "/me/player/devices":
            return _devices_payload({"id": "mac", "name": "Mac", "is_active": True})
        if path == "/me/player/play":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().play(
        name="my sleep playlist", type="playlist", spotify=object()
    )
    assert result == "Now playing: sleep (playlist)"
    assert all(path != "/search" for _, path, _ in calls)
    play = next(call for call in calls if call[1] == "/me/player/play")
    assert play[2]["json"] == {"context_uri": "spotify:playlist:mine"}


@pytest.mark.asyncio
async def test_transfer_playback_resolves_device_by_name(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(client, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/me/player/devices":
            return _devices_payload(
                {"id": "mac", "name": "Geoff's MacBook", "is_active": True},
                {"id": "kitchen", "name": "Kitchen", "is_active": False},
            )
        if path == "/me/player":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().transfer_playback("kitchen", spotify=object())
    assert result == "Playback transferred."
    transfer = next(call for call in calls if call[1] == "/me/player")
    assert transfer[2]["json"] == {"device_ids": ["kitchen"], "play": True}


@pytest.mark.asyncio
async def test_transfer_playback_lists_devices_when_name_is_ambiguous(monkeypatch):
    async def fake_request(client, method, path, **kwargs):
        if path == "/me/player/devices":
            return _devices_payload(
                {"id": "a", "name": "Kitchen speaker", "is_active": False},
                {"id": "b", "name": "Kitchen TV", "is_active": False},
            )
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().transfer_playback("kitchen", spotify=object())
    assert isinstance(result, CapabilityErrorDetail)
    assert "Kitchen speaker" in result.message
    assert "Kitchen TV" in result.message


@pytest.mark.asyncio
async def test_skip_previous_posts_previous_endpoint(monkeypatch):
    calls: list[str] = []

    async def fake_request(client, method, path, **kwargs):
        calls.append(f"{method} {path}")
        return {}

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().skip(direction="previous", spotify=object())
    assert result == "Returned to previous track."
    assert calls == ["POST /me/player/previous"]


@pytest.mark.asyncio
async def test_play_maps_allowlist_403(monkeypatch):
    async def fake_request(client, method, path, **kwargs):
        if path == "/me/player/devices":
            return _devices_payload({"id": "mac", "name": "Mac", "is_active": True})
        raise SpotifyClientError(
            "This Spotify user is not on the app's allowlist. "
            "Add them in the Spotify Developer Dashboard under User Management."
        )

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().play(uri="spotify:track:1", spotify=object())
    assert isinstance(result, CapabilityErrorDetail)
    assert "allowlist" in result.message


@pytest.mark.asyncio
async def test_api_request_maps_403_to_allowlist_message():
    class _Resp:
        status_code = 403
        content = b'{"error":{"status":403,"message":"User not registered"}}'

        def json(self):
            return {"error": {"status": 403, "message": "User not registered"}}

    class _Client:
        async def request(self, *args, **kwargs):
            return _Resp()

    with pytest.raises(SpotifyClientError, match="allowlist"):
        await api_request(_Client(), "GET", "/me/player")


@pytest.mark.asyncio
async def test_api_request_maps_player_404_to_no_device():
    class _Resp:
        status_code = 404
        content = b'{"error":{"status":404,"message":"Not found"}}'

        def json(self):
            return {"error": {"status": 404, "message": "Not found"}}

    class _Client:
        async def request(self, *args, **kwargs):
            return _Resp()

    with pytest.raises(SpotifyClientError, match="devices"):
        await api_request(_Client(), "PUT", "/me/player/pause")


@pytest.mark.asyncio
async def test_api_request_unexpected_status_raises():
    request = httpx.Request("GET", "https://api.spotify.com/v1/search")

    class _Client:
        async def request(self, *args, **kwargs):
            return httpx.Response(500, text="nope", request=request)

    with pytest.raises(httpx.HTTPStatusError):
        await api_request(_Client(), "GET", "/search")


@pytest.mark.asyncio
async def test_queue_without_devices_fails_closed(monkeypatch):
    async def fake_request(client, method, path, **kwargs):
        if path == "/me/player/devices":
            return {"devices": []}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().queue(name="Song", spotify=object())
    assert isinstance(result, CapabilityErrorDetail)
    assert result.message == NO_DEVICE_MESSAGE


@pytest.mark.asyncio
async def test_get_playing_surfaces_allowlist_error(monkeypatch):
    async def fake_request(client, method, path, **kwargs):
        raise SpotifyClientError(ALLOWLIST_MESSAGE)

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().get_playing(spotify=object())
    assert isinstance(result, CapabilityErrorDetail)
    assert "allowlist" in result.message


@pytest.mark.asyncio
async def test_play_radio_by_artist_name_uses_artist_context(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(client, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/search":
            assert kwargs["params"]["type"] == "artist"
            return {
                "artists": {
                    "items": [{"name": "Drake", "uri": "spotify:artist:drake"}]
                }
            }
        if path == "/me/player/devices":
            return _devices_payload({"id": "mac", "name": "Mac", "is_active": True})
        if path == "/me/player/play":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().play(name="Drake radio", spotify=object())
    assert result == "Now playing: Drake (artist). Not a Radio station."
    play = next(call for call in calls if call[1] == "/me/player/play")
    assert play[2]["json"] == {"context_uri": "spotify:artist:drake"}
    assert all(path != "/me/playlists" for _, path, _ in calls)


@pytest.mark.asyncio
async def test_play_radio_of_current_uses_playing_artist(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(client, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/me/player/currently-playing":
            return {
                "item": {
                    "name": "National Treasures",
                    "uri": "spotify:track:1",
                    "artists": [{"name": "Drake", "uri": "spotify:artist:drake"}],
                }
            }
        if path == "/me/player/devices":
            return _devices_payload({"id": "mac", "name": "Mac", "is_active": True})
        if path == "/me/player/play":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().play(name="radio", spotify=object())
    assert result == "Now playing: Drake (artist). Not a Radio station."
    play = next(call for call in calls if call[1] == "/me/player/play")
    assert play[2]["json"] == {"context_uri": "spotify:artist:drake"}
    assert all(path != "/search" for _, path, _ in calls)


@pytest.mark.asyncio
async def test_play_song_radio_with_artist_plays_the_track(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(client, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/search":
            assert kwargs["params"]["type"] == "track"
            return {
                "tracks": {
                    "items": [
                        {
                            "name": "National Treasures",
                            "uri": "spotify:track:nt",
                            "artists": [{"name": "Drake"}],
                        }
                    ]
                }
            }
        if path == "/me/player/devices":
            return _devices_payload({"id": "mac", "name": "Mac", "is_active": True})
        if path == "/me/player/play":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().play(
        name="National Treasures radio", artist="Drake", spotify=object()
    )
    assert result == "Now playing: National Treasures by Drake. Not a Radio station."
    play = next(call for call in calls if call[1] == "/me/player/play")
    assert play[2]["json"] == {"uris": ["spotify:track:nt"]}


@pytest.mark.asyncio
async def test_play_radio_without_seed_or_current_asks_for_name(monkeypatch):
    async def fake_request(client, method, path, **kwargs):
        if path == "/me/player/currently-playing":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().play(name="radio", spotify=object())
    assert isinstance(result, CapabilityErrorDetail)
    assert "Name a song or artist" in result.message


@pytest.mark.asyncio
async def test_save_track_likes_currently_playing(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(client, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/me/player/currently-playing":
            return {
                "item": {
                    "id": "track-1",
                    "name": "Oh Yeah",
                    "uri": "spotify:track:track-1",
                    "artists": [{"name": "Steve Lacy"}],
                }
            }
        if path == "/me/tracks":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().save_track(spotify=object())
    assert "Oh Yeah" in result
    save = next(call for call in calls if call[1] == "/me/tracks")
    assert save[0] == "PUT"
    assert save[2]["params"] == {"ids": "track-1"}


@pytest.mark.asyncio
async def test_add_to_playlist_resolves_library_name_and_current_track(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_request(client, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/me":
            return {"id": "geoff"}
        if path == "/me/playlists":
            return {
                "items": [
                    {
                        "id": "pl-sleep",
                        "name": "sleep",
                        "uri": "spotify:playlist:pl-sleep",
                        "owner": {"id": "geoff"},
                    }
                ]
            }
        if path == "/me/player/currently-playing":
            return {
                "item": {
                    "id": "track-1",
                    "name": "Oh Yeah",
                    "uri": "spotify:track:track-1",
                    "artists": [{"name": "Steve Lacy"}],
                }
            }
        if path == "/playlists/pl-sleep/items":
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr("plugins.spotify.api_request", fake_request)
    result = await SpotifyPlugin().add_to_playlist(playlist="sleep", spotify=object())
    assert "sleep" in result
    add = next(call for call in calls if call[1] == "/playlists/pl-sleep/items")
    assert add[0] == "POST"
    assert add[2]["json"] == {"uris": ["spotify:track:track-1"]}
