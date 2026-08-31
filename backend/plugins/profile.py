"""
Memory plugin for JARV1S.

Core facts: flat list in tool_data, injected into [USER CONTEXT] every turn.
Archival events: timestamped docs in 'memories' collection, searched via embeddings.
"""

import difflib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from pydantic import BaseModel

from core.context import get_owner_id
from core.decorators import tool
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.consent import require_consent
from core.plugins.result import ToolResult
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.plugins.ui import receipt_envelope
from plugins import db
from services.database.mongodb import mongodb, extract_text_content
from services.embeddings import embedding_service


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


logger = logging.getLogger(__name__)

TOOL_NAME = "profile"
MAX_FACTS = 50
MEMORIES_COLLECTION = "memories"
_MIN_SIMILARITY = 0.35
_MAX_RECALL_DOCS = 500


# --- Module-level helpers (called by orchestrator, not exposed to LLM) ---

async def get_profile_block(owner_id: str) -> str | None:
    """
    Build the [USER CONTEXT] prompt block for system prompt injection.
    Reads directly from MongoDB (no context-var dependency).
    Returns None if the user has no stored facts and no cached service identities.
    """
    data = await mongodb.get_tool_data(owner_id, TOOL_NAME)
    facts = data.get("facts", [])

    identities = await mongodb.get_tool_data(owner_id, "service_identities")

    if not facts and not identities:
        return None

    parts: list[str] = ["[USER CONTEXT]"]
    if facts:
        parts.append(". ".join(f["text"] for f in facts) + ".")
    if identities:
        identity_lines = "\n".join(f"- {v}" for v in identities.values())
        parts.append(f"Connected service identities:\n{identity_lines}")

    return "\n\n".join(parts)


async def get_recent_events_block(owner_id: str) -> str | None:
    """
    Build a recent archival events block for subprocess system prompts.

    Subprocess (mode="code") agents have no access to jarvis.* tools and
    cannot call recall(), so recent events are loaded proactively here.
    Returns None if there are no stored events.
    """
    docs = await mongodb.db[MEMORIES_COLLECTION].find(
        {"owner_id": owner_id}
    ).sort("created_at", -1).limit(10).to_list(length=10)
    lines = [doc["event"] for doc in docs if doc.get("event")]
    if not lines:
        return None
    return "[RECENT EVENTS]\n" + "\n".join(f"- {line}" for line in lines)


def _find_match(query: str, facts: List["Fact"]) -> int | None:
    """Return the index of the best fuzzy match, or None."""
    texts = [f.text for f in facts]
    matches = difflib.get_close_matches(query, texts, n=1, cutoff=0.5)
    if matches:
        return texts.index(matches[0])
    return None


class Fact(BaseModel):
    text: str
    added: date
    source: str = "explicit"


class MemoryEntry(BaseModel):
    event: str
    context: str
    created_at: str
    expires_at: str | None = None


class ProfilePlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="profile",
        version="3.0.0",
        description="User memory — core facts and archival event recall.",
        hidden=True,
    )

    # --- Core facts ---

    @tool
    async def add_memory(self, fact: str) -> ToolResult | CapabilityErrorDetail:
        """
        Store one stable, user-scoped fact or preference likely to improve future help.
        Write dense, third-person: "Allergic to shellfish", "Wife is Sarah", "Prefers Celsius".
        Only store clearly stated information likely to remain true next month.
        NOT for events, appointments, or time-bound info — use remember() for those.
        """
        facts = await db.load_models(TOOL_NAME, Fact, key="facts")

        if len(facts) >= MAX_FACTS:
            return _fail(f"Memory full ({MAX_FACTS} facts). Remove an old fact first.")

        facts.append(Fact(text=fact, added=date.today()))
        await db.save_models(TOOL_NAME, facts, key="facts")
        return ToolResult(
            content=f"Stored '{fact}'.",
            ui=[receipt_envelope("Memory Added", fact)],
        )

    @tool
    async def update_memory(self, old: str, new: str) -> ToolResult | CapabilityErrorDetail:
        """
        Update an existing permanent fact when new information refines or corrects it.
        Use instead of add_memory() when the old fact would become stale or duplicated.
        Use the exact text from [USER CONTEXT] for `old`.

        Args:
            old: existing fact text to replace
            new: the updated fact text
        """
        facts = await db.load_models(TOOL_NAME, Fact, key="facts")
        idx = _find_match(old, facts)
        if idx is None:
            return _fail(f"No matching fact found for: {old}")

        replaced = facts[idx].text
        facts[idx] = facts[idx].model_copy(update={"text": new, "added": date.today()})
        await db.save_models(TOOL_NAME, facts, key="facts")
        return ToolResult(
            content=f"Updated '{replaced}' -> '{new}'.",
            ui=[receipt_envelope("Memory Updated", new, sublabel=f"Was: {replaced}")],
        )

    @tool
    async def remove_memory(self, fact: str) -> str | CapabilityErrorDetail:
        """Remove a stored fact. Fuzzy-matches against existing facts."""
        facts = await db.load_models(TOOL_NAME, Fact, key="facts")
        idx = _find_match(fact, facts)
        if idx is None:
            return _fail(f"No matching fact found for: {fact}")

        removed = facts.pop(idx)
        await db.save_models(TOOL_NAME, facts, key="facts")
        return f"Removed '{removed.text}'."

    @tool
    async def clear_memories(self) -> str | CapabilityErrorDetail:
        """
        Erase ALL memory — permanent facts and archived events.
        "Forget everything about me" / "wipe my memory" → this tool.
        To remove a single fact use remove_memory(). To remove a single event use forget().
        Call immediately — the approval system handles confirmation automatically.
        """
        owner_id = get_owner_id()

        async def _do_clear() -> str:
            await db.save_models(TOOL_NAME, [], key="facts")
            result = await mongodb.db[MEMORIES_COLLECTION].delete_many({"owner_id": owner_id})
            return f"All memory cleared — {MAX_FACTS} fact slots freed, {result.deleted_count} events removed."

        return await require_consent(
            "Permanently erase all stored facts and archived events",
            _do_clear,
            detail="Clears the facts list and the memories collection for this user.",
        )

    @tool
    async def get_memories(self) -> List[str]:
        """
        List all stored facts about the user.
        Summarize conversationally — do not read the list mechanically.
        """
        facts = await db.load_models(TOOL_NAME, Fact, key="facts")
        return [f.text for f in facts]

    # --- Archival events ---

    @tool
    async def remember(self, event: str, context: str = "", expires_at: str = "") -> str:
        """
        Log one event, plan, or decision for later recall, never requested future work.
        Requested reminders, tasks, schedules, and habit tracking use their domain tools.
        NOT for permanent identity facts — use add_memory() for those.

        Args:
            expires_at: Optional. ISO-8601 datetime (UTC) after which this event auto-deletes.
                ONLY set when the event has an unambiguous end date (meeting Friday → Saturday midnight).
                Default to OMITTING this — most events should be kept. When in doubt, leave empty.
        """
        owner_id = get_owner_id()
        embedding = embedding_service.embed_one(event)

        doc: Dict[str, Any] = {
            "owner_id": owner_id,
            "event": event,
            "context": context,
            "embedding": embedding,
            "created_at": datetime.now(timezone.utc),
        }

        if expires_at:
            doc["expires_at"] = datetime.fromisoformat(expires_at).replace(tzinfo=timezone.utc)

        await mongodb.db[MEMORIES_COLLECTION].insert_one(doc)

        suffix = f" (expires {expires_at})" if expires_at else ""
        return f"Remembered {event}{suffix}."

    @tool
    async def forget(self, query: str) -> str | CapabilityErrorDetail:
        """
        Remove a single archived event by semantic match.
        "Forget that dentist thing" / "never mind about the Tokyo trip".
        To remove a permanent fact use remove_memory(). To erase everything use clear_memories().
        """
        owner_id = get_owner_id()
        docs = await mongodb.db[MEMORIES_COLLECTION].find(
            {"owner_id": owner_id},
            {"_id": 1, "event": 1, "embedding": 1},
        ).sort("created_at", -1).limit(_MAX_RECALL_DOCS).to_list(
            length=_MAX_RECALL_DOCS
        )

        if not docs:
            return _fail("No archived events to forget.")

        query_vec = embedding_service.embed_one(query)

        best_doc, best_score = None, -1.0
        for doc in docs:
            score = embedding_service.cosine_similarity(query_vec, doc["embedding"])
            if score > best_score:
                best_doc, best_score = doc, score

        if best_doc is None or best_score < 0.4:
            return _fail(f"No matching event found for: {query}")

        await mongodb.db[MEMORIES_COLLECTION].delete_one({"_id": best_doc["_id"]})
        return f"Forgot '{best_doc['event']}'."

    @tool
    async def recall(self, query: str, limit: int = 5) -> List[MemoryEntry]:
        """
        Search past events and conversation history by meaning before reconstructing a memory.
        Use when the user asks about something from a previous session — "did I mention X?",
        "what did we discuss about Y last week?". Searches both archived events and recent
        conversation turns (last 90 days).
        For anything said in the CURRENT session, read the conversation above instead.
        Summarize results conversationally — say "last Tuesday" not raw timestamps.
        """
        owner_id = get_owner_id()
        limit = min(limit, 10)

        # Prefix improves bge-small retrieval accuracy for passage search
        prefixed_query = f"Represent this sentence for searching relevant passages: {query}"
        query_vec = embedding_service.embed_one(prefixed_query)

        scored: list[tuple[dict, float]] = []

        # --- Search archival memories ---
        mem_docs = await mongodb.db[MEMORIES_COLLECTION].find(
            {"owner_id": owner_id},
            {"event": 1, "context": 1, "created_at": 1, "expires_at": 1, "embedding": 1},
        ).sort("created_at", -1).limit(_MAX_RECALL_DOCS).to_list(
            length=_MAX_RECALL_DOCS
        )

        for doc in mem_docs:
            score = embedding_service.cosine_similarity(query_vec, doc["embedding"])
            if score >= _MIN_SIMILARITY:
                scored.append((doc, score))

        # --- Search recent conversation history (user + assistant turns) ---
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        conv_docs = await mongodb.db["conversations"].find(
            {
                "owner_id": owner_id,
                "embedding": {"$exists": True},
                "timestamp": {"$gte": cutoff},
                "role": {"$in": ["user", "assistant"]},
                "metadata.turn_type": {"$ne": "tool_result"},
                "content": {"$not": {"$regex": r"^\s*<tool_result>"}},
            },
            {"content": 1, "timestamp": 1, "embedding": 1, "role": 1},
        ).sort("timestamp", -1).to_list(length=None)

        for doc in conv_docs:
            score = embedding_service.cosine_similarity(query_vec, doc["embedding"])
            if score >= _MIN_SIMILARITY:
                text = extract_text_content(doc["content"])[:200]
                scored.append((
                    {
                        "event": text,
                        "context": f"({doc['role']} turn in conversation)",
                        "created_at": doc["timestamp"],
                        "expires_at": None,
                    },
                    score,
                ))

        scored.sort(key=lambda x: x[1], reverse=True)

        return [
            MemoryEntry(
                event=doc["event"],
                context=doc.get("context", ""),
                created_at=doc["created_at"].isoformat() if hasattr(doc["created_at"], "isoformat") else str(doc["created_at"]),
                expires_at=doc["expires_at"].isoformat() if doc.get("expires_at") else None,
            )
            for doc, _ in scored[:limit]
        ]
