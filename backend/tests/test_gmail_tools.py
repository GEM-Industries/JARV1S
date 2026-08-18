import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.gmail import GmailPlugin, _parse_full_message, _parse_metadata_message


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeGmailClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, path: str, params: dict | None = None) -> FakeResponse:
        self.calls.append((path, params))
        return FakeResponse(self.payload)


def _full_message(message_id: str, body_b64: str) -> dict:
    return {
        "id": message_id,
        "threadId": "thread-1",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "snippet",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Project update"},
                {"name": "From", "value": "Sarah <sarah@example.com>"},
                {"name": "To", "value": "Geoff <geoff@example.com>"},
                {"name": "Date", "value": "Thu, 21 May 2026 10:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": body_b64},
        },
    }


def test_parse_metadata_message_includes_triage_fields():
    summary = _parse_metadata_message(
        {
            "id": "msg-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX", "UNREAD", "IMPORTANT"],
            "snippet": "Can you review this before standup?",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Review before standup"},
                    {"name": "From", "value": "Sarah <sarah@example.com>"},
                    {"name": "To", "value": "Geoff <geoff@example.com>"},
                    {"name": "Cc", "value": "Team <team@example.com>"},
                    {"name": "Date", "value": "Thu, 21 May 2026 10:00:00 +0000"},
                ]
            },
        }
    )

    assert summary.id == "msg-1"
    assert summary.thread_id == "thread-1"
    assert summary.subject == "Review before standup"
    assert summary.sender == "Sarah <sarah@example.com>"
    assert summary.to == ["Geoff <geoff@example.com>"]
    assert summary.cc == ["Team <team@example.com>"]
    assert summary.labels == ["INBOX", "UNREAD", "IMPORTANT"]
    assert summary.is_unread is True


def test_parse_full_message_extracts_body_text():
    detail = _parse_full_message(_full_message("msg-1", "SGVsbG8gdGhlcmU="))

    assert detail.id == "msg-1"
    assert detail.thread_id == "thread-1"
    assert detail.subject == "Project update"
    assert detail.body_text == "Hello there"
    assert detail.is_unread is True


@pytest.mark.asyncio
async def test_get_thread_full_fetches_all_message_bodies():
    gmail = FakeGmailClient(
        {
            "messages": [
                _full_message("msg-1", "Rmlyc3QgbWVzc2FnZQ=="),
                _full_message("msg-2", "U2Vjb25kIG1lc3NhZ2U="),
            ]
        }
    )

    result = await GmailPlugin().get_thread_full("thread-1", gmail=gmail)

    assert gmail.calls == [("/users/me/threads/thread-1", {"format": "full"})]
    assert result.thread_id == "thread-1"
    assert result.message_count == 2
    assert [message.body_text for message in result.messages] == [
        "First message",
        "Second message",
    ]


@pytest.mark.asyncio
async def test_get_inbox_returns_complete_empty_evidence():
    gmail = FakeGmailClient({"messages": []})

    result = await GmailPlugin().get_inbox(gmail=gmail)

    assert result.emails == []
    assert result.match_status == "none"
    assert result.coverage == "complete"


@pytest.mark.asyncio
async def test_get_inbox_surfaces_partial_message_fetch():
    class PartialGmailClient:
        async def get(self, path: str, params=None):
            if path == "/users/me/messages":
                return FakeResponse({"messages": [{"id": "msg-1"}, {"id": "msg-2"}]})
            if path.endswith("/msg-1"):
                return FakeResponse(_full_message("msg-1", "SGVsbG8="))
            raise RuntimeError("message fetch failed")

    result = await GmailPlugin().get_inbox(gmail=PartialGmailClient())

    assert [email.id for email in result.emails] == ["msg-1"]
    assert result.failed_message_count == 1
    assert result.truncated is False
    assert result.coverage == "partial"


@pytest.mark.asyncio
async def test_get_inbox_truncation_is_partial_coverage():
    class TruncatedGmailClient:
        async def get(self, path: str, params=None):
            if path == "/users/me/messages":
                return FakeResponse(
                    {
                        "messages": [{"id": "msg-1"}],
                        "nextPageToken": "page-2",
                    }
                )
            if path.endswith("/msg-1"):
                return FakeResponse(_full_message("msg-1", "SGVsbG8="))
            raise AssertionError(f"unexpected path: {path}")

    result = await GmailPlugin().get_inbox(gmail=TruncatedGmailClient(), max_results=1)

    assert [email.id for email in result.emails] == ["msg-1"]
    assert result.truncated is True
    assert result.failed_message_count == 0
    assert result.coverage == "partial"


@pytest.mark.asyncio
async def test_archive_resolves_and_checks_target_before_mutation(invoke_tool):
    gmail = SimpleNamespace(
        get=AsyncMock(return_value=FakeResponse(_full_message("msg-1", "SGVsbG8="))),
        post=AsyncMock(return_value=FakeResponse({})),
    )

    result = await invoke_tool(
        GmailPlugin(),
        "archive_email",
        "msg-1",
        expected_subject="Project update",
        expected_sender="sarah@example.com",
        gmail=gmail,
    )

    assert result.content.startswith("Archived")
    gmail.get.assert_awaited_once()
    gmail.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_archive_guard_mismatch_does_not_mutate(invoke_tool):
    gmail = SimpleNamespace(
        get=AsyncMock(return_value=FakeResponse(_full_message("msg-1", "SGVsbG8="))),
        post=AsyncMock(return_value=FakeResponse({})),
    )

    result = await invoke_tool(
        GmailPlugin(),
        "archive_email",
        "msg-1",
        expected_subject="Invoice",
        gmail=gmail,
    )

    assert result.message.startswith("Refusing to modify email")
    gmail.post.assert_not_awaited()
