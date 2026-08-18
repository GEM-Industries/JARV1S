"""
Gmail Plugin for JARV1S.

Provides inbox queries, message read/send/reply, drafts, label management,
and inbox summary widgets. Uses the shared Google OAuth app with scope
aggregation via IntegrationManager + AuthManager.
"""

import asyncio
import base64
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from core.decorators import tool
from core.plugins.consent import require_consent
from core.plugins.read_evidence import MatchStatus, ReadCoverage, match_status_from_count
from core.plugins.result import ToolResult
from core.plugins.types import JarvisPlugin, PluginMetadata, UIEnvelope, WidgetLayout, WidgetSize
from core.plugins.ui import content_envelope, push_ui, receipt_envelope
from core.plugins.capabilities import CapabilityErrorDetail


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Return type models
# ---------------------------------------------------------------------------

class AttachmentInfo(BaseModel):
    filename: str
    mime_type: str
    size: int


class EmailSummary(BaseModel):
    id: str
    thread_id: str
    subject: str
    sender: str       # "Name <email>" or bare email
    to: List[str]
    cc: List[str]
    bcc: List[str]
    snippet: str
    date: str         # ISO 8601
    labels: List[str]
    is_unread: bool


class EmailDetail(BaseModel):
    id: str
    thread_id: str
    subject: str
    sender: str
    to: List[str]
    cc: List[str]
    bcc: List[str]
    snippet: str
    date: str
    labels: List[str]
    is_unread: bool
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    attachments: List[AttachmentInfo] = []


class ThreadView(BaseModel):
    thread_id: str
    subject: str
    message_count: int
    messages: List[EmailSummary]


class ThreadDetail(BaseModel):
    thread_id: str
    subject: str
    message_count: int
    messages: List[EmailDetail]


class SendConfirmation(BaseModel):
    id: str
    thread_id: str
    to: str
    subject: str


class EmailQueryResult(BaseModel):
    emails: List[EmailSummary] = Field(default_factory=list)
    match_status: MatchStatus
    coverage: ReadCoverage
    truncated: bool = False
    failed_message_count: int = 0
    query: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_METADATA_HEADERS = ["Subject", "From", "To", "Cc", "Bcc", "Date", "In-Reply-To", "References", "Message-ID"]

# Gmail API requires metadataHeaders as repeated query params, not comma-separated.
# httpx accepts a list of (key, value) tuples for this.
_METADATA_PARAMS: list[tuple[str, str]] = [
    ("format", "metadata"),
    *[("metadataHeaders", h) for h in _METADATA_HEADERS],
]

# Cap concurrent per-message fetches to avoid hitting Gmail's 250 quota-units/sec limit.
_FETCH_SEMAPHORE = asyncio.Semaphore(10)


def _get_header(headers: List[Dict[str, str]], name: str) -> str:
    """Extract a header value by name (case-insensitive)."""
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _decode_b64(data: str) -> str:
    """Decode a base64url-encoded Gmail body part."""
    if not data:
        return ""
    # Pad to a multiple of 4 before decoding
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Recursively extract (plain_text, html) from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})

    if mime == "text/plain":
        return _decode_b64(body.get("data", "")), None
    if mime == "text/html":
        return None, _decode_b64(body.get("data", ""))

    plain, html = None, None
    for part in payload.get("parts", []):
        p, h = _extract_body(part)
        if p and not plain:
            plain = p
        if h and not html:
            html = h
    return plain, html


def _extract_attachments(payload: Dict[str, Any]) -> List[AttachmentInfo]:
    """Recursively extract attachment metadata from a Gmail message payload."""
    attachments: List[AttachmentInfo] = []
    filename = payload.get("filename", "")
    body = payload.get("body", {})

    if filename and body.get("attachmentId"):
        attachments.append(AttachmentInfo(
            filename=filename,
            mime_type=payload.get("mimeType", "application/octet-stream"),
            size=body.get("size", 0),
        ))

    for part in payload.get("parts", []):
        attachments.extend(_extract_attachments(part))

    return attachments


def _parse_date(raw: str) -> str:
    """Parse a Gmail header Date string into ISO 8601. Falls back to raw on failure."""
    try:
        return parsedate_to_datetime(raw).isoformat()
    except Exception:
        return raw


def _internal_date_to_iso(internal_date: str) -> str:
    """Convert Gmail internalDate (milliseconds since epoch) to ISO 8601."""
    try:
        ts = int(internal_date) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return internal_date


def _parse_metadata_message(msg: Dict[str, Any]) -> EmailSummary:
    """Parse a Gmail message returned with format=metadata into an EmailSummary."""
    headers = msg.get("payload", {}).get("headers", [])
    subject = _get_header(headers, "Subject") or "(No subject)"
    sender = _get_header(headers, "From") or ""
    to_raw = _get_header(headers, "To")
    cc_raw = _get_header(headers, "Cc")
    bcc_raw = _get_header(headers, "Bcc")
    date_header = _get_header(headers, "Date")
    date = _parse_date(date_header) if date_header else _internal_date_to_iso(msg.get("internalDate", "0"))

    labels = msg.get("labelIds", [])
    return EmailSummary(
        id=msg["id"],
        thread_id=msg.get("threadId", ""),
        subject=subject,
        sender=sender,
        to=[a.strip() for a in to_raw.split(",") if a.strip()] if to_raw else [],
        cc=[a.strip() for a in cc_raw.split(",") if a.strip()] if cc_raw else [],
        bcc=[a.strip() for a in bcc_raw.split(",") if a.strip()] if bcc_raw else [],
        snippet=msg.get("snippet", ""),
        date=date,
        labels=labels,
        is_unread="UNREAD" in labels,
    )


def _inbox_envelope(summaries: List[EmailSummary]) -> UIEnvelope:
    unread_count = sum(1 for s in summaries if s.is_unread)
    return UIEnvelope(
        widget_id="gmail-inbox",
        component="InboxWidget",
        title="Inbox",
        layout=WidgetLayout(size=WidgetSize.LARGE_WIDE, priority=4),
        data={
            "unread_count": unread_count,
            "emails": [s.model_dump() for s in summaries],
        },
    )


def _parse_full_message(msg: Dict[str, Any]) -> EmailDetail:
    """Parse a Gmail message returned with format=full into an EmailDetail."""
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    labels = msg.get("labelIds", [])

    date_header = _get_header(headers, "Date")
    date = _parse_date(date_header) if date_header else _internal_date_to_iso(msg.get("internalDate", "0"))
    to_raw = _get_header(headers, "To")
    cc_raw = _get_header(headers, "Cc")
    bcc_raw = _get_header(headers, "Bcc")
    body_text, body_html = _extract_body(payload)

    return EmailDetail(
        id=msg["id"],
        thread_id=msg.get("threadId", ""),
        subject=_get_header(headers, "Subject") or "(No subject)",
        sender=_get_header(headers, "From") or "",
        to=[a.strip() for a in to_raw.split(",") if a.strip()] if to_raw else [],
        cc=[a.strip() for a in cc_raw.split(",") if a.strip()] if cc_raw else [],
        bcc=[a.strip() for a in bcc_raw.split(",") if a.strip()] if bcc_raw else [],
        snippet=msg.get("snippet", ""),
        date=date,
        labels=labels,
        is_unread="UNREAD" in labels,
        body_text=body_text,
        body_html=body_html,
        attachments=_extract_attachments(payload),
    )


async def _fetch_message_summaries(
    gmail: httpx.AsyncClient,
    message_ids: List[str],
) -> tuple[List[EmailSummary], int]:
    """Batch-fetch metadata for a list of message IDs, capped at 10 concurrent."""

    async def _get_one(msg_id: str) -> Optional[EmailSummary]:
        async with _FETCH_SEMAPHORE:
            try:
                resp = await gmail.get(f"/users/me/messages/{msg_id}", params=_METADATA_PARAMS)
                resp.raise_for_status()
                return _parse_metadata_message(resp.json())
            except Exception as e:
                logger.warning("Failed to fetch message %s: %s", msg_id, e)
                return None

    results = await asyncio.gather(*[_get_one(mid) for mid in message_ids])
    summaries = [r for r in results if r is not None]
    return summaries, len(results) - len(summaries)


def _build_raw_message(
    to: str,
    subject: str,
    body: str,
    sender: str = "me",
    cc: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> str:
    """Build a base64url-encoded RFC 2822 message for the Gmail send endpoint."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if sender != "me":
        msg["From"] = sender
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class GmailPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="gmail",
        version="1.0.0",
        description="Gmail: inbox queries, send/reply, drafts, label management, and inbox summaries.",
        dependencies=["httpx"],
        utterances=[
            "check my email inbox",
            "do I have any new emails",
            "any unread messages in my inbox",
            "show me emails from Sarah",
            "send an email to Dave about the project",
            "reply to that email thread",
            "draft an email to the team",
            "archive that email from marketing",
            "give me an inbox summary",
            "search my email for the invoice",
            "mark that message as read",
            "what's in my inbox today",
            "do I have any important emails this morning",
            "read the latest email from my boss",
        ],
    )

    async def register_integrations(self) -> None:
        from core.integrations import integrations
        from plugins.gmail.client import GMAIL_SCOPES, create_gmail_client, refresh_gmail_client

        integrations.register(
            "gmail",
            create_gmail_client,
            refresh=refresh_gmail_client,
            provider="google",
            required_scopes=GMAIL_SCOPES,
        )

    # -----------------------------------------------------------------------
    # Tools
    # -----------------------------------------------------------------------

    @tool(inject=["gmail"])
    async def get_inbox(
        self,
        unread_only: bool = True,
        max_results: int = 10,
        gmail: httpx.AsyncClient = None,
    ) -> EmailQueryResult:
        """
        Get recent inbox message metadata only. Use for first-pass triage, then fetch
        selected full bodies with get_email(). Metadata includes subject, sender,
        recipients, snippet, date, labels, unread state, message_id, and thread_id.
        Returns EmailQueryResult with emails plus match_status/coverage evidence.
        """
        query = "in:inbox is:unread" if unread_only else "in:inbox"
        resp = await gmail.get(
            "/users/me/messages",
            params={"q": query, "maxResults": min(max_results, 50)},
        )
        resp.raise_for_status()
        data = resp.json()
        message_ids = [m["id"] for m in data.get("messages", [])]
        truncated = bool(data.get("nextPageToken"))
        if not message_ids:
            result = EmailQueryResult(
                emails=[],
                match_status=MatchStatus.NONE,
                coverage=ReadCoverage.COMPLETE,
                truncated=False,
                query=query,
            )
            push_ui(_inbox_envelope([]))
            return result
        summaries, failed = await _fetch_message_summaries(gmail, message_ids)
        summaries.sort(key=lambda m: m.date, reverse=True)
        coverage = (
            ReadCoverage.PARTIAL if truncated or failed else ReadCoverage.COMPLETE
        )
        push_ui(_inbox_envelope(summaries))
        return EmailQueryResult(
            emails=summaries,
            match_status=match_status_from_count(len(summaries)),
            coverage=coverage,
            truncated=truncated,
            failed_message_count=failed,
            query=query,
        )

    @tool(inject=["gmail"])
    async def get_email(
        self,
        message_id: str,
        gmail: httpx.AsyncClient = None,
    ) -> EmailDetail:
        """
        Get the full body and metadata of one email by message_id. Use after
        get_inbox() or search_emails() selects a candidate; do not fetch every
        body during first-pass triage unless the user explicitly asks.
        """
        resp = await gmail.get(f"/users/me/messages/{message_id}", params={"format": "full"})
        resp.raise_for_status()
        return _parse_full_message(resp.json())

    @tool(inject=["gmail"])
    async def search_emails(
        self,
        query: str,
        max_results: int = 10,
        gmail: httpx.AsyncClient = None,
    ) -> List[EmailSummary]:
        """
        Search email metadata using Gmail syntax. Useful targeted queries include
        'is:unread newer_than:2d', 'from:name@example.com', 'subject:invoice',
        and 'has:attachment'. Returns EmailSummary objects for triage.
        """
        resp = await gmail.get(
            "/users/me/messages",
            params={"q": query, "maxResults": min(max_results, 50)},
        )
        resp.raise_for_status()
        data = resp.json()
        message_ids = [m["id"] for m in data.get("messages", [])]
        if not message_ids:
            return []
        summaries, _failed = await _fetch_message_summaries(gmail, message_ids)
        summaries.sort(key=lambda m: m.date, reverse=True)
        return summaries

    @tool(inject=["gmail"])
    async def get_thread(
        self,
        thread_id: str,
        gmail: httpx.AsyncClient = None,
    ) -> ThreadView:
        """
        Get metadata for all messages in a conversation thread by thread_id.
        Use for efficient thread triage; call get_thread_full() only when the
        full body of every message is needed.
        """
        resp = await gmail.get(
            f"/users/me/threads/{thread_id}",
            params=_METADATA_PARAMS,
        )
        resp.raise_for_status()
        data = resp.json()

        raw_messages = data.get("messages", [])
        messages = [_parse_metadata_message(m) for m in raw_messages]

        subject = messages[0].subject if messages else "(No subject)"
        return ThreadView(
            thread_id=thread_id,
            subject=subject,
            message_count=len(messages),
            messages=messages,
        )

    @tool(inject=["gmail"])
    async def get_thread_full(
        self,
        thread_id: str,
        gmail: httpx.AsyncClient = None,
    ) -> ThreadDetail:
        """
        Get full bodies and metadata for every email in a thread. Use only after
        get_thread() or search results show the whole conversation is needed.
        """
        resp = await gmail.get(
            f"/users/me/threads/{thread_id}",
            params={"format": "full"},
        )
        resp.raise_for_status()
        data = resp.json()
        messages = [_parse_full_message(m) for m in data.get("messages", [])]
        subject = messages[0].subject if messages else "(No subject)"
        return ThreadDetail(
            thread_id=thread_id,
            subject=subject,
            message_count=len(messages),
            messages=messages,
        )

    @tool(inject=["gmail"])
    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        gmail: httpx.AsyncClient = None,
    ) -> SendConfirmation:
        """
        Compose and send an email. Call this when the user has requested sending.
        If it returns APPROVAL_NEEDED, the email has not sent yet. Report sent
        only after the final tool/approval result confirms sending.
        Never invent email addresses — only use addresses the user provides explicitly.
        """
        raw = _build_raw_message(to=to, subject=subject, body=body, cc=cc)
        send_body: Dict[str, Any] = {"raw": raw}

        async def _do_send() -> SendConfirmation:
            resp = await gmail.post("/users/me/messages/send", json=send_body)
            resp.raise_for_status()
            data = resp.json()
            return SendConfirmation(
                id=data.get("id", ""),
                thread_id=data.get("threadId", ""),
                to=to,
                subject=subject,
            )

        return await require_consent(
            f"Send email to {to}: {subject}",
            _do_send,
            detail=body[:200] + ("…" if len(body) > 200 else ""),
        )

    @tool(inject=["gmail"])
    async def reply_to_thread(
        self,
        thread_id: str,
        body: str,
        gmail: httpx.AsyncClient = None,
    ) -> SendConfirmation:
        """
        Reply to an existing email thread. Call this when the user has requested
        sending the reply. If it returns APPROVAL_NEEDED, the reply has not sent
        yet. Report sent only after the final tool/approval result confirms sending.
        Fetches thread metadata automatically to set the correct reply headers.
        """
        # Fetch the latest message in the thread to get reply headers
        thread_resp = await gmail.get(
            f"/users/me/threads/{thread_id}",
            params=_METADATA_PARAMS,
        )
        thread_resp.raise_for_status()
        thread_data = thread_resp.json()

        messages = thread_data.get("messages", [])
        if not messages:
            raise ValueError(f"Thread {thread_id} has no messages.")

        last_msg = messages[-1]
        last_headers = last_msg.get("payload", {}).get("headers", [])
        original_from = _get_header(last_headers, "From")
        original_subject = _get_header(last_headers, "Subject") or ""
        message_id_header = _get_header(last_headers, "Message-ID")
        references = _get_header(last_headers, "References")

        # Reply subject: keep Re: prefix convention
        reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"

        # Build References chain
        new_references = f"{references} {message_id_header}".strip() if references else message_id_header

        raw = _build_raw_message(
            to=original_from,
            subject=reply_subject,
            body=body,
            in_reply_to=message_id_header,
            references=new_references,
        )

        send_body: Dict[str, Any] = {"raw": raw, "threadId": thread_id}

        async def _do_reply() -> SendConfirmation:
            resp = await gmail.post("/users/me/messages/send", json=send_body)
            resp.raise_for_status()
            data = resp.json()
            return SendConfirmation(
                id=data.get("id", ""),
                thread_id=data.get("threadId", thread_id),
                to=original_from,
                subject=reply_subject,
            )

        return await require_consent(
            f"Reply to {original_from}: {reply_subject}",
            _do_reply,
            detail=body[:200] + ("…" if len(body) > 200 else ""),
        )

    @tool(inject=["gmail"])
    async def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        gmail: httpx.AsyncClient = None,
    ) -> ToolResult:
        """
        Save an email as a draft without sending.
        This tool displays the exact recipient, subject, and body for review.
        Never invent email addresses; use an address the user provided or one
        found via Gmail search and shown to the user.
        """
        raw = _build_raw_message(to=to, subject=subject, body=body, cc=cc)
        resp = await gmail.post("/users/me/drafts", json={"message": {"raw": raw}})
        resp.raise_for_status()
        draft_id = resp.json().get("id", "")
        pairs = {"To": to, "Subject": subject, "Draft ID": draft_id}
        if cc:
            pairs["Cc"] = cc
        envelope = content_envelope(
            title="Email Draft",
            sections=[
                {"type": "kv", "pairs": pairs},
                {"type": "markdown", "content": body},
            ],
            size=WidgetSize.LARGE_WIDE,
        )
        return ToolResult(
            content=f"Draft saved (to: {to}, subject: {subject}, draft_id: {draft_id}).",
            ui=[envelope],
        )

    @tool(inject=["gmail"])
    async def archive_email(
        self,
        message_id: str,
        expected_subject: Optional[str] = None,
        expected_sender: Optional[str] = None,
        gmail: httpx.AsyncClient = None,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Archive an email by removing it from the inbox (removes INBOX label).
        Get message_id from get_inbox() or search_emails() first.
        Pass expected_subject / expected_sender from the message you meant to archive.
        """
        resp = await gmail.get(
            f"/users/me/messages/{message_id}",
            params=_METADATA_PARAMS,
        )
        resp.raise_for_status()
        summary = _parse_metadata_message(resp.json())
        if expected_subject and expected_subject.lower() not in summary.subject.lower():
            return _fail(
                "Refusing to modify email; target subject did not match. "
                f"Fetched '{summary.subject}', expected '{expected_subject}'."
            )
        if expected_sender and expected_sender.lower() not in summary.sender.lower():
            return _fail(
                "Refusing to modify email; target sender did not match. "
                f"Fetched '{summary.sender}', expected '{expected_sender}'."
            )

        resp = await gmail.post(
            f"/users/me/messages/{message_id}/modify",
            json={"removeLabelIds": ["INBOX"]},
        )
        resp.raise_for_status()
        return ToolResult(
            content=f"Archived message {message_id}.",
            ui=[receipt_envelope("Email Archived", message_id)],
        )

    @tool(inject=["gmail"])
    async def mark_read(
        self,
        message_id: str,
        unread: bool = False,
        gmail: httpx.AsyncClient = None,
    ) -> ToolResult:
        """
        Mark an email as read (default) or unread (unread=True).
        Get message_id from get_inbox() or search_emails() first.
        """
        if unread:
            body = {"addLabelIds": ["UNREAD"], "removeLabelIds": []}
        else:
            body = {"removeLabelIds": ["UNREAD"], "addLabelIds": []}

        resp = await gmail.post(f"/users/me/messages/{message_id}/modify", json=body)
        resp.raise_for_status()
        state = "unread" if unread else "read"
        return ToolResult(
            content=f"Marked message {message_id} as {state}.",
            ui=[receipt_envelope("Email Updated", f"Marked {state}", sublabel=message_id)],
        )
