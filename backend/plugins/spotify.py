"""
Spotify Plugin — bespoke wrapper over the Composio MCP transport.

Reuses Composio OAuth but adds docstrings that teach the LLM correct
behavior (e.g. prefer user library over public search). The auto-bridged
MCPBridgePlugin for "spotify" is skipped because bespoke plugins have priority.
"""

import logging
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from core.decorators import tool
from core.integrations.mcp.bridge import _unwrap_composio_response
from core.integrations.mcp.client import MCPError
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.types import JarvisPlugin, PluginMetadata

logger = logging.getLogger(__name__)


class SpotifyView(BaseModel):
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class SpotifyAck(SpotifyView):
    success: bool
    message: str | None = None
    error: str | None = None


class SpotifyQueueAck(SpotifyAck):
    queued: list[str] = Field(default_factory=list)
    note: str | None = None


class SpotifyItem(SpotifyView):
    type: str
    id: str | None = None
    name: str
    uri: str | None = None
    artist: str | None = None
    album: str | None = None
    owner: str | None = None


class SpotifySearchResults(SpotifyView):
    type: str
    results: list[SpotifyItem] = Field(default_factory=list)


class SpotifyDevice(SpotifyView):
    id: str | None = None
    name: str
    type: str | None = None
    is_active: bool = False
    volume_percent: int | None = None


class SpotifyDevices(SpotifyView):
    devices: list[SpotifyDevice] = Field(default_factory=list)


class SpotifyPlaying(SpotifyView):
    is_playing: bool = False
    name: str | None = None
    artist: str | None = None
    album: str | None = None
    id: str | None = None
    uri: str | None = None
    progress_ms: int | None = None
    duration_ms: int | None = None
    device: str | None = None
    queue: list[SpotifyItem] = Field(default_factory=list)


class SpotifyQueue(SpotifyView):
    currently_playing: SpotifyItem | None = None
    queue: list[SpotifyItem] = Field(default_factory=list)


def _build_search_query(name: str, artist: Optional[str], type: str) -> str:
    """Build a Spotify search query using field filters when an artist is provided."""
    if not artist:
        return name
    if type == "track":
        return f"track:{name} artist:{artist}"
    return f"{name} artist:{artist}"


def _artist_name(item: dict[str, Any]) -> str | None:
    artists = item.get("artists") or []
    if artists and isinstance(artists[0], dict):
        return artists[0].get("name")
    return None


def _spotify_item(item: dict[str, Any] | None, item_type: str = "track") -> SpotifyItem | None:
    if not isinstance(item, dict) or not item:
        return None
    owner = item.get("owner") or {}
    album = item.get("album") or {}
    return SpotifyItem(
        type=item.get("type") or item_type,
        id=item.get("id"),
        name=item.get("name") or "Unknown",
        uri=item.get("uri"),
        artist=_artist_name(item),
        album=album.get("name") if isinstance(album, dict) else None,
        owner=owner.get("display_name") if isinstance(owner, dict) else None,
    )


def _ack_from_result(result: Any, message: str) -> SpotifyAck:
    if isinstance(result, dict) and result.get("error"):
        return SpotifyAck(success=False, error=str(result["error"]))
    return SpotifyAck(success=True, message=message)


def _search_results(result: Any, item_type: str) -> SpotifySearchResults:
    items = []
    if isinstance(result, dict):
        raw_items = ((result.get(f"{item_type}s") or {}).get("items") or [])
        items = [
            view for raw in raw_items
            if (view := _spotify_item(raw, item_type)) is not None
        ]
    return SpotifySearchResults(type=item_type, results=items)


class SpotifyPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="spotify",
        version="1.0.0",
        description="Spotify playback, library, and queue control.",
        composio_app="spotify",
        utterances=[
            "play music",
            "play my playlist",
            "play song",
            "spotify",
            "pause",
            "skip",
            "next song",
            "previous song",
            "go back",
            "what's playing",
            "volume",
            "queue",
            "find some music",
            "find music for a mood",
            "add a song to the queue",
            "play focus music",
            "shuffle",
            "repeat",
            "save this song",
            "add to playlist",
            "play on my speaker",
        ],
    )

    async def _mcp(self, tool_name: str, **kwargs: Any) -> Any:
        from core.integrations.composio_gateway import get_composio_gateway

        gw = get_composio_gateway()
        if not gw:
            return {"error": "Composio gateway not configured"}
        filtered = {k: v for k, v in kwargs.items() if v is not None}
        raw = await gw.call_mcp_tool("spotify", tool_name, filtered)
        result = _unwrap_composio_response(raw)
        if isinstance(result, CapabilityErrorDetail):
            return {"error": result.message}
        return result

    async def _devices(self) -> list[dict[str, Any]]:
        result = await self._mcp("SPOTIFY_GET_AVAILABLE_DEVICES")
        if isinstance(result, dict):
            return [device for device in (result.get("devices") or []) if isinstance(device, dict)]
        return []

    async def _resolve_device(self, device: str | None = None) -> str | SpotifyAck | None:
        devices = await self._devices()
        if not devices:
            return None
        if not device:
            active = next((item for item in devices if item.get("is_active")), None)
            return (active or devices[0]).get("id")
        needle = device.lower()
        matches = [
            item for item in devices
            if needle in (item.get("name") or "").lower()
        ]
        names = ", ".join(item.get("name") or "Unknown" for item in (matches or devices))
        if not matches:
            return SpotifyAck(
                success=False,
                error=f"No Spotify device matching {device!r}. Use one of: {names}",
            )
        if len(matches) > 1:
            return SpotifyAck(
                success=False,
                error=f"Multiple Spotify devices match {device!r}: {names}",
            )
        return matches[0].get("id")

    # -- Playback control --

    @tool
    async def play(
        self,
        name: Optional[str] = None,
        artist: Optional[str] = None,
        type: str = "playlist",
        uri: Optional[str] = None,
        device: Optional[str] = None,
    ) -> SpotifyAck:
        """
        Play a playlist, album, artist, or track. Resumes playback if all args are omitted.
        Pass `name` for any play request — library lookup and search are handled internally.
        Only pass `uri` when you already have a Spotify URI from a prior lookup.
        Pass `device` as a speaker or computer name to start on that player.

        Args:
            name: Name to play (e.g. "My Playlist", "Bohemian Rhapsody"). Omit only if using uri.
            artist: Artist name for targeted search (e.g. "Queen"). Omit for playlists/albums.
            type: One of "playlist", "album", "artist", "track". Default "playlist".
            uri: Spotify URI for direct playback (e.g. "spotify:track:..."). Skips search.
            device: Target device name. Omit for the active player.
        """
        if uri:
            desc = uri
            kw = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
        elif name is None:
            desc = "resumed playback"
            kw: dict = {}
        else:
            match = None
            if type == "playlist":
                playlists = await self._mcp("SPOTIFY_GET_CURRENT_USER_S_PLAYLISTS", limit=50)
                items = [p for p in ((playlists or {}).get("items") or []) if p]
                match = next((p for p in items if name.lower() in (p.get("name") or "").lower()), None)

            if not match:
                q = _build_search_query(name, artist, type)
                results = await self._mcp("SPOTIFY_SEARCH_FOR_ITEM", q=q, type=[type], limit=5)
                search_items = [i for i in ((results or {}).get(type + "s", {}).get("items", [])) if i]
                if not search_items:
                    return SpotifyAck(success=False, error=f"No {type} found matching '{name}'")
                match = next(
                    (i for i in search_items if name.lower() in (i.get("name") or "").lower()),
                    search_items[0],
                )

            artists = match.get("artists") or []
            artist_str = f" by {artists[0]['name']}" if artists else ""
            desc = f"{match.get('name', name)}{artist_str}"
            if type != "track":
                desc += f" ({type})"
            kw = {"uris": [match["uri"]]} if type == "track" else {"context_uri": match["uri"]}

        device_id = await self._resolve_device(device)
        if isinstance(device_id, SpotifyAck):
            return device_id
        if not device_id:
            return SpotifyAck(success=False, error="No Spotify devices available — open Spotify on a device first")

        try:
            raw = await self._mcp("SPOTIFY_START_RESUME_PLAYBACK", device_id=device_id, **kw)
        except MCPError as exc:
            return SpotifyAck(success=False, error=str(exc))

        if isinstance(raw, dict) and raw.get("error"):
            return SpotifyAck(success=False, error=str(raw["error"]))
        return SpotifyAck(success=True, message=f"Now playing: {desc}")

    @tool
    async def pause(self) -> SpotifyAck:
        """Pause Spotify playback."""
        return _ack_from_result(
            await self._mcp("SPOTIFY_PAUSE_PLAYBACK"),
            "Playback paused.",
        )

    @tool
    async def skip(self, direction: Literal["next", "previous"] = "next") -> SpotifyAck:
        """Skip to the next or previous track. Default is next."""
        if direction == "previous":
            return _ack_from_result(
                await self._mcp("SPOTIFY_SKIP_TO_PREVIOUS"),
                "Returned to previous track.",
            )
        return _ack_from_result(
            await self._mcp("SPOTIFY_SKIP_TO_NEXT"),
            "Skipped to next track.",
        )

    @tool
    async def shuffle(self, state: bool = True) -> SpotifyAck:
        """Toggle shuffle. True enables, False disables."""
        return _ack_from_result(
            await self._mcp("SPOTIFY_TOGGLE_PLAYBACK_SHUFFLE", state=state),
            f"Shuffle {'enabled' if state else 'disabled'}.",
        )

    @tool
    async def repeat(self, state: str = "track") -> SpotifyAck:
        """
        Set repeat mode. "track" repeats the current song, "context" repeats the
        playlist/album, "off" disables repeat.
        """
        return _ack_from_result(
            await self._mcp("SPOTIFY_SET_REPEAT_MODE", state=state),
            f"Repeat mode set to {state}.",
        )

    @tool
    async def set_volume(self, volume_percent: int) -> SpotifyAck:
        """
        Set playback volume (0-100).

        Args:
            volume_percent: Volume level, 0 (mute) to 100 (max).
        """
        return _ack_from_result(
            await self._mcp(
                "SPOTIFY_SET_PLAYBACK_VOLUME",
                volume_percent=volume_percent,
            ),
            f"Volume set to {volume_percent}%.",
        )

    @tool
    async def transfer_playback(self, device: str, start_playing: bool = True) -> SpotifyAck:
        """
        Move what's already playing to another device (e.g. "play on the kitchen speaker").
        Pass the speaker or computer name; do not look up a device id first.
        """
        device_id = await self._resolve_device(device)
        if isinstance(device_id, SpotifyAck):
            return device_id
        if not device_id:
            return SpotifyAck(success=False, error="No Spotify devices available — open Spotify on a device first")
        return _ack_from_result(
            await self._mcp(
                "SPOTIFY_TRANSFER_PLAYBACK",
                device_ids=[device_id],
                play=start_playing,
            ),
            "Playback transferred.",
        )

    # -- Queue & now-playing --

    @tool
    async def get_playing(self) -> SpotifyPlaying:
        """Get the currently playing track — name, artist, album, progress, device, and a short upcoming queue."""
        result = await self._mcp("SPOTIFY_GET_CURRENTLY_PLAYING_TRACK")
        item = (result or {}).get("item") if isinstance(result, dict) else {}
        device = (result or {}).get("device") if isinstance(result, dict) else {}
        album = (item or {}).get("album") or {}
        queue_result = await self._mcp("SPOTIFY_GET_THE_USER_S_QUEUE")
        upcoming: list[SpotifyItem] = []
        if isinstance(queue_result, dict):
            upcoming = [
                view for raw in (queue_result.get("queue") or [])[:5]
                if isinstance(raw, dict)
                if (view := _spotify_item(raw, raw.get("type", "track"))) is not None
            ]
        return SpotifyPlaying(
            is_playing=bool((result or {}).get("is_playing")) if isinstance(result, dict) else False,
            name=(item or {}).get("name"),
            artist=_artist_name(item or {}),
            album=album.get("name") if isinstance(album, dict) else None,
            id=(item or {}).get("id"),
            uri=(item or {}).get("uri"),
            progress_ms=(result or {}).get("progress_ms") if isinstance(result, dict) else None,
            duration_ms=(item or {}).get("duration_ms"),
            device=device.get("name") if isinstance(device, dict) else None,
            queue=upcoming,
        )

    @tool
    async def queue(
        self,
        name: Optional[str] = None,
        artist: Optional[str] = None,
        count: int = 1,
        uri: Optional[str] = None,
    ) -> SpotifyQueueAck:
        """
        Queue tracks — search and add to the playback queue (plays after the current track).
        Pass `uri` for a direct Spotify link, or `name` for a search-based queue.

        Use count=1 for a specific song (e.g. name="Bohemian Rhapsody", artist="Queen").
        Use count=3-5 for a vibe/genre/artist request (e.g. name="indie rock").

        Args:
            name: Track name or descriptive query. Omit when using uri.
            artist: Artist name for targeted search. Omit for genre/mood queries.
            count: How many tracks to queue (1-5). Default 1. Ignored when using uri.
            uri: Spotify URI (e.g. "spotify:track:...") or track URL. Skips search.
        """
        device_id = await self._resolve_device()
        if isinstance(device_id, SpotifyAck):
            return SpotifyQueueAck(success=False, error=device_id.error)

        if uri:
            if "open.spotify.com/track/" in uri:
                track_id = uri.split("open.spotify.com/track/")[1].split("?")[0]
                uri = f"spotify:track:{track_id}"
            try:
                await self._mcp("SPOTIFY_ADD_ITEM_TO_PLAYBACK_QUEUE", uri=uri, device_id=device_id)
            except MCPError as exc:
                return SpotifyQueueAck(success=False, error=f"Failed to queue: {exc}")
            return SpotifyQueueAck(success=True, queued=[uri])

        if not name:
            return SpotifyQueueAck(success=False, error="Provide either name or uri")

        clamped = max(1, min(5, count))
        q = _build_search_query(name, artist, "track")
        results = await self._mcp("SPOTIFY_SEARCH_FOR_ITEM", q=q, type=["track"], limit=clamped)
        items = [i for i in ((results or {}).get("tracks", {}).get("items", [])) if i]
        if not items and artist:
            # Artist field may be a subtitle (e.g. "From 'Howl's Moving Castle'"); retry without it
            results = await self._mcp("SPOTIFY_SEARCH_FOR_ITEM", q=name, type=["track"], limit=clamped)
            items = [i for i in ((results or {}).get("tracks", {}).get("items", [])) if i]
        if not items:
            return SpotifyQueueAck(success=False, error=f"No tracks found for '{name}'")

        queued: list[str] = []
        for track in items[:clamped]:
            uri = track.get("uri")
            if not uri:
                continue
            try:
                await self._mcp("SPOTIFY_ADD_ITEM_TO_PLAYBACK_QUEUE", uri=uri, device_id=device_id)
            except MCPError as exc:
                return SpotifyQueueAck(
                    success=False,
                    error=f"Failed to queue '{track.get('name','?')}': {exc}",
                    queued=queued,
                )
            artists = track.get("artists") or []
            artist_str = f" by {artists[0]['name']}" if artists else ""
            queued.append(f"{track.get('name','?')}{artist_str}")

        note = None
        if count > clamped:
            note = f"Requested {count} but max is {clamped}."
        return SpotifyQueueAck(success=True, queued=queued, note=note)

    # -- Library --

    @tool
    async def get_my_playlists(self, limit: int = 20) -> SpotifySearchResults:
        """
        List the user's own Spotify playlists (owned and followed).
        Use to inspect or list playlists — play(name=..., type='playlist') checks the library automatically.
        Returns dict with "items" list; each item has "name", "uri", "id" keys.
        """
        result = await self._mcp("SPOTIFY_GET_CURRENT_USER_S_PLAYLISTS", limit=limit)
        items = [
            view for raw in (result.get("items", []) if isinstance(result, dict) else [])
            if (view := _spotify_item(raw, "playlist")) is not None
        ]
        return SpotifySearchResults(type="playlist", results=items)

    @tool
    async def save_track(self, ids: List[str]) -> SpotifyAck:
        """
        Save one or more tracks to the user's library ("like" them).
        Get track IDs from get_playing() or search() results.

        Args:
            ids: List of Spotify track IDs (not full URIs).
        """
        return _ack_from_result(
            await self._mcp("SPOTIFY_SAVE_TRACKS_FOR_CURRENT_USER", ids=ids),
            "Track saved.",
        )

    @tool
    async def add_to_playlist(self, playlist_id: str, uris: List[str]) -> SpotifyAck:
        """
        Add tracks to one of the user's playlists.
        Call get_my_playlists() first to find the playlist_id.

        Args:
            playlist_id: Spotify playlist ID (from get_my_playlists).
            uris: List of Spotify URIs to add (e.g. ["spotify:track:..."]).
        """
        return _ack_from_result(
            await self._mcp(
                "SPOTIFY_ADD_ITEMS_TO_PLAYLIST", playlist_id=playlist_id, uris=uris,
            ),
            "Tracks added to playlist.",
        )

    # -- Discovery --

    @tool
    async def search(self, q: str, type: str = "track", limit: int = 5) -> SpotifySearchResults:
        """
        Search the public Spotify catalog for tracks, albums, artists, or playlists.
        Use only when you need catalog data or a URI — to play by name, use play(name=...) directly.
        For the user's own playlists, use get_my_playlists() instead.
        Returns dict with a "{type}s" key containing an "items" list; each item has "name", "uri", "id" keys.

        Args:
            q: Search query (song name, artist, etc.)
            type: One of "track", "album", "artist", "playlist". Default "track".
            limit: Max results (1-50). Default 5.
        """
        return _search_results(
            await self._mcp("SPOTIFY_SEARCH_FOR_ITEM", q=q, type=[type], limit=limit),
            type,
        )
