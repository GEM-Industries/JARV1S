from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.watchers.gmail import GmailWatcher


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeCursors:
    def __init__(self, history_id: str | None = None):
        self.doc = {"source": "gmail", "history_id": history_id} if history_id else None

    async def find_one(self, query: dict) -> dict | None:
        return self.doc

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> None:
        current = self.doc or {"source": query["source"]}
        self.doc = {**current, **update["$set"]}


def _metadata_message(message_id: str, sender: str = "Ada <ada@example.com>") -> dict:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": ["INBOX"],
        "snippet": "hello",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "From", "value": sender},
                {"name": "Date", "value": "Wed, 2 Sep 2026 11:00:00 +0000"},
            ]
        },
    }


def _history(*message_ids: str, history_id: str = "200", next_page: str | None = None) -> dict:
    payload: dict = {
        "historyId": history_id,
        "history": [
            {"messagesAdded": [{"message": {"id": message_id}}]}
            for message_id in message_ids
        ],
    }
    if next_page:
        payload["nextPageToken"] = next_page
    return payload


class FakeGmail:
    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, path: str, params=None) -> FakeResponse:
        self.calls.append(path)
        if path in self.routes:
            return self.routes[path]
        raise AssertionError(f"unexpected path: {path}")


def _patch_watcher(monkeypatch, *, gmail, cursors: FakeCursors) -> None:
    monkeypatch.setattr("core.integrations.integrations.get", AsyncMock(return_value=gmail))
    monkeypatch.setattr(
        "services.database.mongodb.mongodb",
        SimpleNamespace(db=SimpleNamespace(watcher_cursors=cursors)),
    )


@pytest.mark.asyncio
async def test_poll_returns_summaries_and_advances_cursor(monkeypatch):
    gmail = FakeGmail(
        {
            "/users/me/history": FakeResponse(_history("msg-new", history_id="200")),
            "/users/me/messages/msg-new": FakeResponse(_metadata_message("msg-new")),
        }
    )
    cursors = FakeCursors("100")
    _patch_watcher(monkeypatch, gmail=gmail, cursors=cursors)

    items = await GmailWatcher().poll()

    assert [item["id"] for item in items] == ["msg-new"]
    assert items[0]["sender"] == "Ada <ada@example.com>"
    assert cursors.doc["history_id"] == "200"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history_response",
    [
        FakeResponse({}, status_code=404),
        FakeResponse(_history("msg-old", history_id="999", next_page="page-2")),
    ],
)
async def test_poll_rebases_when_history_window_cannot_be_consumed(
    monkeypatch, history_response
):
    gmail = FakeGmail(
        {
            "/users/me/history": history_response,
            "/users/me/profile": FakeResponse({"historyId": "1000"}),
        }
    )
    cursors = FakeCursors("100")
    _patch_watcher(monkeypatch, gmail=gmail, cursors=cursors)

    items = await GmailWatcher().poll()

    assert items == []
    assert not any(path.startswith("/users/me/messages/") for path in gmail.calls)
    assert cursors.doc["history_id"] == "1000"


@pytest.mark.asyncio
async def test_poll_advances_cursor_when_history_has_no_new_mail(monkeypatch):
    gmail = FakeGmail(
        {"/users/me/history": FakeResponse({"historyId": "201", "history": []})}
    )
    cursors = FakeCursors("100")
    _patch_watcher(monkeypatch, gmail=gmail, cursors=cursors)

    items = await GmailWatcher().poll()

    assert items == []
    assert cursors.doc["history_id"] == "201"


@pytest.mark.asyncio
async def test_first_poll_establishes_baseline_without_backfill(monkeypatch):
    gmail = FakeGmail({"/users/me/profile": FakeResponse({"historyId": "50"})})
    cursors = FakeCursors(None)
    _patch_watcher(monkeypatch, gmail=gmail, cursors=cursors)

    items = await GmailWatcher().poll()

    assert items == []
    assert cursors.doc["history_id"] == "50"


@pytest.mark.asyncio
async def test_poll_propagates_history_api_errors(monkeypatch):
    class Boom:
        async def get(self, path, params=None):
            raise RuntimeError("gmail down")

    _patch_watcher(monkeypatch, gmail=Boom(), cursors=FakeCursors("100"))

    with pytest.raises(RuntimeError, match="gmail down"):
        await GmailWatcher().poll()
