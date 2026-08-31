"""
Spotify Plugin — playback, library, and queue control via the Web API.
"""

import re
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from core.decorators import tool
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.types import JarvisPlugin, PluginMetadata
from plugins.spotify.client import (
    NO_DEVICE_MESSAGE,
    SPOTIFY_SCOPES,
    SpotifyClientError,
    api_request,
    create_spotify_client,
    refresh_spotify_client,
)


class SpotifyQueueAck(BaseModel):
    queued: list[str] = Field(default_factory=list)
    note: str | None = None


class SpotifyItem(BaseModel):
    type: str
    id: str | None = None
    name: str
    uri: str | None = None
    artist: str | None = None
    album: str | None = None
    owner: str | None = None


class SpotifySearchResults(BaseModel):
    type: str
    results: list[SpotifyItem] = Field(default_factory=list)


class SpotifyPlaying(BaseModel):
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


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


_RADIO_TOKEN = re.compile(r"\b(radio|stations?)\b", re.I)
_RADIO_NEED_SEED = (
    "Name a song or artist. Spotify Radio stations cannot be started — "
    "I can play that artist, or the track."
)


def _radio_seed(name: str | None) -> tuple[str | None, bool]:
    if not name or not name.strip():
        return None, False
    compact = " ".join(name.split())
    radio = bool(_RADIO_TOKEN.search(compact))
    if not radio:
        return compact, False
    seed = " ".join(_RADIO_TOKEN.sub(" ", compact).split()).strip(" -")
    return (seed or None), True


def _primary_artist(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    first = (item.get("artists") or [None])[0]
    return first if isinstance(first, dict) else None


def _artist_name(item: dict[str, Any] | None) -> str | None:
    artist = _primary_artist(item)
    return artist.get("name") if artist else None


def _track_label(item: dict[str, Any]) -> str:
    name = item.get("name") or "the track"
    artist = _artist_name(item)
    return f"{name} by {artist}" if artist else name


def _build_search_query(name: str, artist: Optional[str], item_type: str) -> str:
    if not artist:
        return name
    if item_type == "track":
        return f"track:{name} artist:{artist}"
    return f"{name} artist:{artist}"


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


async def _mutate_ack(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    ok: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
) -> str | CapabilityErrorDetail:
    try:
        await api_request(client, method, path, params=params, json=json)
        return ok
    except SpotifyClientError as exc:
        return _fail(exc.message)


def _search_results(result: Any, item_type: str) -> SpotifySearchResults:
    raw_items = ((result or {}).get(f"{item_type}s") or {}).get("items") or []
    items = [
        view for raw in raw_items
        if (view := _spotify_item(raw, item_type)) is not None
    ]
    return SpotifySearchResults(type=item_type, results=items)


def _playlist_query(name: str) -> str:
    query = " ".join(name.strip().split())
    while True:
        stripped = re.sub(r"^(my|the)\s+", "", query, flags=re.I)
        stripped = re.sub(r"\s+playlists?$", "", stripped, flags=re.I).strip()
        if stripped == query:
            return query or name.strip()
        query = stripped


def _playlist_rank(name: str, query: str) -> int | None:
    hay = name.lower().strip()
    needle = query.lower().strip()
    if not hay or not needle:
        return None
    if hay == needle:
        return 0
    if hay.startswith(needle):
        return 1
    if needle in hay:
        return 2
    return None


def _pick_named(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    best: tuple[int, dict[str, Any]] | None = None
    for row in items:
        rank = _playlist_rank(row.get("name") or "", name)
        if rank is None:
            continue
        if best is None or rank < best[0]:
            best = (rank, row)
    if best:
        return best[1]
    return items[0] if items else None


async def _currently_playing(client: httpx.AsyncClient) -> dict[str, Any]:
    return (await api_request(client, "GET", "/me/player/currently-playing")) or {}


async def _playing_item(client: httpx.AsyncClient) -> dict[str, Any] | None:
    item = (await _currently_playing(client)).get("item")
    if isinstance(item, dict) and item.get("uri"):
        return item
    return None


async def _search_items(
    client: httpx.AsyncClient,
    name: str,
    artist: str | None,
    item_type: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    q = _build_search_query(name, artist, item_type)
    results = await api_request(
        client, "GET", "/search", params={"q": q, "type": item_type, "limit": limit}
    )
    return [row for row in ((results or {}).get(f"{item_type}s") or {}).get("items") or [] if row]


async def _resolve_track(
    client: httpx.AsyncClient,
    name: str | None,
    artist: str | None,
) -> dict[str, Any] | CapabilityErrorDetail:
    if name:
        track = _pick_named(await _search_items(client, name, artist, "track"), name)
        if not track:
            return _fail(f"No track found matching '{name}'.", code="not_found")
        return track
    track = await _playing_item(client)
    if not track:
        return _fail("Nothing is playing — name a song.", code="not_found")
    return track


async def _library_playlist(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    query = _playlist_query(name)
    me = await api_request(client, "GET", "/me")
    user_id = (me or {}).get("id")
    best: tuple[int, int, dict[str, Any]] | None = None
    offset = 0
    while offset < 150:
        result = await api_request(
            client, "GET", "/me/playlists", params={"limit": 50, "offset": offset}
        )
        items = [row for row in ((result or {}).get("items") or []) if row]
        if not items:
            break
        for playlist in items:
            rank = _playlist_rank(playlist.get("name") or "", query)
            if rank is None:
                continue
            owner_id = (playlist.get("owner") or {}).get("id")
            owned_penalty = 0 if user_id and owner_id == user_id else 1
            candidate = (rank, owned_penalty, playlist)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if len(items) < 50:
            break
        offset += 50
    return None if best is None else best[2]


class SpotifyPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="spotify",
        version="1.0.0",
        description="Spotify playback, library, and queue control.",
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
            "like this song",
            "add this to my playlist",
            "play radio",
            "play on my speaker",
        ],
    )

    async def register_integrations(self) -> None:
        from core.integrations import integrations

        integrations.register(
            "spotify",
            create_spotify_client,
            refresh=refresh_spotify_client,
            provider="spotify",
            required_scopes=SPOTIFY_SCOPES,
        )

    async def _devices(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        result = await api_request(client, "GET", "/me/player/devices")
        return [device for device in (result.get("devices") or []) if isinstance(device, dict)]

    async def _resolve_device(
        self,
        client: httpx.AsyncClient,
        device: str | None = None,
    ) -> str | CapabilityErrorDetail:
        devices = await self._devices(client)
        if not devices:
            return _fail(NO_DEVICE_MESSAGE)
        if not device:
            picked = next((item for item in devices if item.get("is_active")), None) or devices[0]
            device_id = picked.get("id")
            return device_id if device_id else _fail(NO_DEVICE_MESSAGE)
        needle = device.lower()
        matches = [
            item for item in devices
            if needle in (item.get("name") or "").lower()
        ]
        names = ", ".join(item.get("name") or "Unknown" for item in (matches or devices))
        if not matches:
            return _fail(
                f"No Spotify device matching {device!r}. Use one of: {names}",
                code="not_found",
            )
        if len(matches) > 1:
            return _fail(
                f"Multiple Spotify devices match {device!r}: {names}",
                code="invalid_arguments",
            )
        device_id = matches[0].get("id")
        return device_id if device_id else _fail(NO_DEVICE_MESSAGE)

    @tool(inject=["spotify"])
    async def play(
        self,
        name: Optional[str] = None,
        artist: Optional[str] = None,
        type: str = "playlist",
        uri: Optional[str] = None,
        device: Optional[str] = None,
        spotify: httpx.AsyncClient = None,
    ) -> str | CapabilityErrorDetail:
        """
        Play a playlist, album, artist, or track. Resume if all args are omitted.
        Named playback goes through `name` — library then catalog, in this tool. Do not list
        or search first. Songs: type="track" and artist when known. Mood/genre: type="playlist".
        Radio/station is this tool, not a station API — play the artist or the seed track;
        omit name to use what's playing. Say what started, not that a Radio station began.
        Pass `uri` only for a Spotify link the user gave.

        Args:
            name: What to play. "my X playlist" → X; filler is stripped in code.
            artist: Artist for a track/album. Omit for playlists and mood queries.
            type: "playlist" (default), "album", "artist", or "track".
            uri: Spotify URI. Skips library/catalog lookup.
            device: Speaker or computer name. Omit for the active player.
        """
        try:
            seed, radio = _radio_seed(name)
            play_type = type
            if radio and play_type == "playlist":
                play_type = "track" if artist else "artist"
            lookup = seed if radio else name
            if radio and not lookup and artist:
                lookup, artist, play_type = artist, None, "artist"

            if uri:
                desc = uri
                body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
            elif not lookup:
                if not radio:
                    desc = "resumed playback"
                    body = None
                else:
                    current = await _playing_item(spotify)
                    artist_obj = _primary_artist(current)
                    artist_uri = artist_obj.get("uri") if artist_obj else None
                    if not artist_obj or not artist_uri:
                        return _fail(_RADIO_NEED_SEED, code="invalid_arguments")
                    desc = f"{artist_obj.get('name') or 'the current artist'} (artist)"
                    body = {"context_uri": artist_uri}
            else:
                match = None
                search_name = _playlist_query(lookup) if play_type == "playlist" else lookup
                if play_type == "playlist":
                    match = await _library_playlist(spotify, lookup)
                if not match:
                    search_items = await _search_items(spotify, search_name, artist, play_type)
                    if not search_items and radio and play_type == "artist":
                        play_type = "track"
                        search_items = await _search_items(
                            spotify, search_name, artist, play_type
                        )
                    if not search_items:
                        return _fail(
                            f"No {play_type} found matching '{lookup}'",
                            code="not_found",
                        )
                    match = _pick_named(search_items, search_name) or search_items[0]
                match_uri = match.get("uri")
                if not match_uri:
                    return _fail(
                        f"No {play_type} found matching '{lookup}'",
                        code="not_found",
                    )
                desc = (
                    _track_label(match)
                    if play_type == "track"
                    else f"{match.get('name', lookup)} ({play_type})"
                )
                body = {"uris": [match_uri]} if play_type == "track" else {"context_uri": match_uri}

            device_id = await self._resolve_device(spotify, device)
            if isinstance(device_id, CapabilityErrorDetail):
                return device_id

            await api_request(
                spotify,
                "PUT",
                "/me/player/play",
                params={"device_id": device_id},
                json=body,
            )
            message = f"Now playing: {desc}"
            if radio:
                message += ". Not a Radio station."
            return message
        except SpotifyClientError as exc:
            return _fail(exc.message)

    @tool(inject=["spotify"])
    async def pause(self, spotify: httpx.AsyncClient = None) -> str | CapabilityErrorDetail:
        """Pause Spotify playback."""
        return await _mutate_ack(spotify, "PUT", "/me/player/pause", "Playback paused.")

    @tool(inject=["spotify"])
    async def skip(
        self,
        direction: Literal["next", "previous"] = "next",
        spotify: httpx.AsyncClient = None,
    ) -> str | CapabilityErrorDetail:
        """Skip to the next or previous track. Default is next."""
        if direction == "previous":
            return await _mutate_ack(
                spotify, "POST", "/me/player/previous", "Returned to previous track."
            )
        return await _mutate_ack(spotify, "POST", "/me/player/next", "Skipped to next track.")

    @tool(inject=["spotify"])
    async def shuffle(self, state: bool = True, spotify: httpx.AsyncClient = None) -> str | CapabilityErrorDetail:
        """Toggle shuffle. True enables, False disables."""
        return await _mutate_ack(
            spotify,
            "PUT",
            "/me/player/shuffle",
            f"Shuffle {'enabled' if state else 'disabled'}.",
            params={"state": state},
        )

    @tool(inject=["spotify"])
    async def repeat(self, state: str = "track", spotify: httpx.AsyncClient = None) -> str | CapabilityErrorDetail:
        """
        Set repeat mode. "track" repeats the current song, "context" repeats the
        playlist/album, "off" disables repeat.
        """
        return await _mutate_ack(
            spotify, "PUT", "/me/player/repeat", f"Repeat mode set to {state}.", params={"state": state}
        )

    @tool(inject=["spotify"])
    async def set_volume(
        self, volume_percent: int, spotify: httpx.AsyncClient = None
    ) -> str | CapabilityErrorDetail:
        """
        Set playback volume (0-100).

        Args:
            volume_percent: Volume level, 0 (mute) to 100 (max).
        """
        return await _mutate_ack(
            spotify,
            "PUT",
            "/me/player/volume",
            f"Volume set to {volume_percent}%.",
            params={"volume_percent": volume_percent},
        )

    @tool(inject=["spotify"])
    async def transfer_playback(
        self,
        device: str,
        start_playing: bool = True,
        spotify: httpx.AsyncClient = None,
    ) -> str | CapabilityErrorDetail:
        """
        Move what's already playing to another device (e.g. "play on the kitchen speaker").
        Pass the speaker or computer name; do not look up a device id first.
        """
        try:
            device_id = await self._resolve_device(spotify, device)
            if isinstance(device_id, CapabilityErrorDetail):
                return device_id
            await api_request(
                spotify,
                "PUT",
                "/me/player",
                json={"device_ids": [device_id], "play": start_playing},
            )
            return "Playback transferred."
        except SpotifyClientError as exc:
            return _fail(exc.message)

    @tool(inject=["spotify"])
    async def get_playing(
        self, spotify: httpx.AsyncClient = None
    ) -> SpotifyPlaying | CapabilityErrorDetail:
        """Get the currently playing track — name, artist, album, progress, device, and a short upcoming queue."""
        try:
            result = await _currently_playing(spotify)
        except SpotifyClientError as exc:
            return _fail(exc.message)
        item = result.get("item") or {}
        device = result.get("device") or {}
        album = item.get("album") or {}
        upcoming: list[SpotifyItem] = []
        try:
            queue_result = await api_request(spotify, "GET", "/me/player/queue")
            upcoming = [
                view for raw in ((queue_result or {}).get("queue") or [])[:5]
                if isinstance(raw, dict)
                if (view := _spotify_item(raw, raw.get("type", "track"))) is not None
            ]
        except SpotifyClientError:
            upcoming = []
        return SpotifyPlaying(
            is_playing=bool(result.get("is_playing")),
            name=item.get("name"),
            artist=_artist_name(item),
            album=album.get("name") if isinstance(album, dict) else None,
            id=item.get("id"),
            uri=item.get("uri"),
            progress_ms=result.get("progress_ms"),
            duration_ms=item.get("duration_ms"),
            device=device.get("name") if isinstance(device, dict) else None,
            queue=upcoming,
        )

    @tool(inject=["spotify"])
    async def queue(
        self,
        name: Optional[str] = None,
        artist: Optional[str] = None,
        count: int = 1,
        uri: Optional[str] = None,
        spotify: httpx.AsyncClient = None,
    ) -> SpotifyQueueAck | CapabilityErrorDetail:
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
        try:
            device_id = await self._resolve_device(spotify)
            if isinstance(device_id, CapabilityErrorDetail):
                return device_id
            params = {"device_id": device_id}

            if uri:
                if "open.spotify.com/track/" in uri:
                    track_id = uri.split("open.spotify.com/track/")[1].split("?")[0]
                    uri = f"spotify:track:{track_id}"
                await api_request(
                    spotify, "POST", "/me/player/queue", params={"uri": uri, **params}
                )
                return SpotifyQueueAck(queued=[uri])

            if not name:
                return _fail("Provide either name or uri.", code="invalid_arguments")

            clamped = max(1, min(5, count))
            items = await _search_items(spotify, name, artist, "track", limit=clamped)
            if not items and artist:
                items = await _search_items(spotify, name, None, "track", limit=clamped)
            if not items:
                return _fail(f"No tracks found for '{name}'", code="not_found")

            queued: list[str] = []
            for track in items[:clamped]:
                track_uri = track.get("uri")
                if not track_uri:
                    continue
                await api_request(
                    spotify,
                    "POST",
                    "/me/player/queue",
                    params={"uri": track_uri, **params},
                )
                queued.append(_track_label(track))

            note = None
            if count > clamped:
                note = f"Requested {count} but max is {clamped}."
            return SpotifyQueueAck(queued=queued, note=note)
        except SpotifyClientError as exc:
            return _fail(exc.message)

    @tool(inject=["spotify"])
    async def get_my_playlists(
        self, limit: int = 20, spotify: httpx.AsyncClient = None
    ) -> SpotifySearchResults | CapabilityErrorDetail:
        """
        List playlists the user owns or follows. Use to answer "what playlists do I have".
        Do not call this to start playback — play(name=..., type="playlist") resolves the library.
        """
        try:
            result = await api_request(
                spotify, "GET", "/me/playlists", params={"limit": limit}
            )
        except SpotifyClientError as exc:
            return _fail(exc.message)
        items = [
            view for raw in ((result or {}).get("items") or [])
            if (view := _spotify_item(raw, "playlist")) is not None
        ]
        return SpotifySearchResults(type="playlist", results=items)

    @tool(inject=["spotify"])
    async def save_track(
        self,
        name: Optional[str] = None,
        artist: Optional[str] = None,
        spotify: httpx.AsyncClient = None,
    ) -> str | CapabilityErrorDetail:
        """
        Like the current song, or a named track. Omit name to save what's playing.
        Do not call get_playing or search first.
        """
        try:
            track = await _resolve_track(spotify, name, artist)
            if isinstance(track, CapabilityErrorDetail):
                return track
            track_id = track.get("id")
            if not track_id:
                return _fail("No track found to save.", code="not_found")
            await api_request(spotify, "PUT", "/me/tracks", params={"ids": track_id})
            return f"Saved {_track_label(track)}."
        except SpotifyClientError as exc:
            return _fail(exc.message)

    @tool(inject=["spotify"])
    async def add_to_playlist(
        self,
        playlist: str,
        name: Optional[str] = None,
        artist: Optional[str] = None,
        spotify: httpx.AsyncClient = None,
    ) -> str | CapabilityErrorDetail:
        """
        Add the current song, or a named track, to a library playlist by name.
        "add this to my sleep playlist" → playlist="sleep". Do not list playlists or copy IDs.
        """
        try:
            target = await _library_playlist(spotify, playlist)
            if not target or not target.get("id"):
                return _fail(f"No library playlist matching '{playlist}'.", code="not_found")
            track = await _resolve_track(spotify, name, artist)
            if isinstance(track, CapabilityErrorDetail):
                return track
            track_uri = track.get("uri")
            if not track_uri:
                return _fail("No track found to add.", code="not_found")
            await api_request(
                spotify,
                "POST",
                f"/playlists/{target['id']}/items",
                json={"uris": [track_uri]},
            )
            return f"Added {_track_label(track)} to {target.get('name') or playlist}."
        except SpotifyClientError as exc:
            return _fail(exc.message)

    @tool(inject=["spotify"])
    async def search(
        self,
        q: str,
        type: str = "track",
        limit: int = 5,
        spotify: httpx.AsyncClient = None,
    ) -> SpotifySearchResults | CapabilityErrorDetail:
        """
        Search the public catalog to answer what's available. Do not search in order to play —
        play(name=...) looks up the library and catalog itself. User playlists are not this tool.

        Args:
            q: Search query (song name, artist, etc.)
            type: One of "track", "album", "artist", "playlist". Default "track".
            limit: Max results (1-50). Default 5.
        """
        try:
            result = await api_request(
                spotify,
                "GET",
                "/search",
                params={"q": q, "type": type, "limit": limit},
            )
        except SpotifyClientError as exc:
            return _fail(exc.message)
        return _search_results(result, type)
