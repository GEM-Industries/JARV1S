"""GmailWatcher — incremental Gmail inbox sync via historyId cursor.

Gmail automations observe mail that arrives while JARV1S is watching
("when X emails me"). They are not an inbox replay. historyId is a mailbox
watermark, not a per-message offset, so a window is either consumed whole
or skipped — never sliced.

Lifecycle
---------
First poll (no cursor)
    Persist users.getProfile historyId and return []. No backfill.

Subsequent polls
    history.list since the cursor. A complete INBOX messagesAdded page is
    returned as summaries and the cursor advances to that page's historyId.

Unconsumable window (historyId 404, or nextPageToken)
    Same as first poll: rebase to now and return []. The gap is dropped
    rather than drained. A page token means we were behind by more than
    one history page (app off, expired cursor, or a stall) — not a burst
    to rate-limit.

Cursor is persisted on watcher_cursors so restarts stay incremental.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class GmailWatcher:
    """
    Polls for newly delivered inbox messages using Gmail's history.list API.

    Each dict has fields matching EmailSummary (id, thread_id, subject,
    sender, snippet, date, labels, is_unread). Rules condition on any field,
    e.g.: {"field": "sender", "op": "contains", "value": "boss@company.com"}
    """

    source = "gmail"
    trigger_mode = "reactive"
    trigger_events = [
        {
            "event": "",
            "description": (
                "Triggers when a new email arrives in your Gmail inbox. "
                "Built-in — no external connection required. "
                "Filter by sender, subject, or labels via conditions."
            ),
        }
    ]
    condition_fields = [
        {"field": "subject", "type": "string"},
        {"field": "sender", "type": "string", "hint": "'Name <email>' format — use contains"},
        {"field": "snippet", "type": "string"},
        {"field": "labels", "type": "string", "hint": "use contains to match a label"},
        {"field": "is_unread", "type": "boolean", "hint": "equals 'true' or 'false'"},
    ]

    async def poll(self) -> list[dict]:
        from core.integrations import integrations
        from core.integrations.manager import NeedsReauth

        try:
            gmail = await integrations.get("gmail")
        except (KeyError, NeedsReauth):
            logger.debug("Gmail integration not configured, skipping poll.")
            return []

        cursor = await self._load_cursor()
        if cursor is None:
            await self._reset_cursor(gmail)
            return []

        new_ids, latest_history_id = await self._fetch_new_message_ids(gmail, cursor)
        if not new_ids:
            if latest_history_id:
                await self._save_cursor(latest_history_id)
            return []

        from plugins.gmail import _fetch_message_summaries
        summaries, failed = await _fetch_message_summaries(gmail, new_ids)
        if failed:
            logger.warning(
                "Gmail watcher: advancing cursor past %d message(s) that failed to fetch",
                failed,
            )
        if latest_history_id:
            await self._save_cursor(latest_history_id)
        return [summary.model_dump() for summary in summaries]

    async def _fetch_new_message_ids(
        self, gmail, start_history_id: str
    ) -> tuple[list[str], str | None]:
        """Return (new_message_ids, latest_historyId).

        404 or a truncated page re-establishes the baseline and returns ([], None).
        Other HTTP/network errors propagate to AutomationService.
        """
        resp = await gmail.get(
            "/users/me/history",
            params={
                "startHistoryId": start_history_id,
                "historyTypes": "messageAdded",
                "labelId": "INBOX",
            },
        )
        if resp.status_code == 404:
            logger.warning(
                "Gmail historyId expired (cursor=%s) — re-establishing baseline",
                start_history_id,
            )
            await self._reset_cursor(gmail)
            return [], None
        resp.raise_for_status()

        data = resp.json()
        if data.get("nextPageToken"):
            logger.warning(
                "Gmail watcher: history window truncated (cursor=%s) — re-establishing baseline",
                start_history_id,
            )
            await self._reset_cursor(gmail)
            return [], None

        seen: set[str] = set()
        ids: list[str] = []
        for record in data.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added.get("message", {}).get("id")
                if msg_id and msg_id not in seen:
                    seen.add(msg_id)
                    ids.append(msg_id)
        return ids, data.get("historyId")

    async def _reset_cursor(self, gmail) -> None:
        resp = await gmail.get("/users/me/profile")
        resp.raise_for_status()
        history_id = str(resp.json()["historyId"])
        await self._save_cursor(history_id)
        logger.info("Gmail watcher: baseline cursor established (historyId=%s)", history_id)

    async def _load_cursor(self) -> str | None:
        from services.database.mongodb import mongodb
        try:
            doc = await mongodb.db.watcher_cursors.find_one({"source": self.source})
            return doc["history_id"] if doc else None
        except Exception as e:
            logger.warning("Gmail watcher: failed to load cursor: %s", e)
            return None

    async def _save_cursor(self, history_id: str) -> None:
        from services.database.mongodb import mongodb
        try:
            await mongodb.db.watcher_cursors.update_one(
                {"source": self.source},
                {"$set": {"history_id": history_id, "updated_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except Exception as e:
            logger.warning("Gmail watcher: failed to save cursor: %s", e)
