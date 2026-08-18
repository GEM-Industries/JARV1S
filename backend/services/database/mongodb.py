from typing import Any, Optional, Dict, List, Union
import logging
from datetime import datetime, timedelta, timezone
from bson.codec_options import CodecOptions
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import ConnectionFailure

from core.config import settings

logger = logging.getLogger(__name__)


def extract_text_content(content: Any) -> str:
    """Normalize message content (str or multipart list) to plain text."""
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
    return str(content).strip()


class MongoDBService:
    def __init__(self):
        self.client: Optional[AsyncIOMotorClient] = None
        self.db: Optional[AsyncIOMotorDatabase] = None

    async def connect(self) -> None:
        """Connect to MongoDB."""
        try:
            self.client = AsyncIOMotorClient(settings.MONGODB_URL)
            self.db = self.client.get_database(
                settings.DATABASE_NAME,
                codec_options=CodecOptions(tz_aware=True),
            )
            # Verify connection
            await self.client.admin.command('ping')
            await self._ensure_indexes()
            logger.info("Successfully connected to MongoDB")
        except ConnectionFailure as e:
            logger.error(f"Could not connect to MongoDB: {e}")
            raise

    async def _ensure_indexes(self) -> None:
        await self.db.protocols.create_index("id", unique=True)
        await self.db.protocols.create_index([("name", 1), ("owner_id", 1)])
        await self.db.protocol_runs.create_index([("owner_id", 1), ("protocol_name", 1), ("started_at", -1)])
        await self.db.protocol_runs.create_index("turn_id", sparse=True)
        # TTL: auto-delete protocol run records after 90 days
        await self.db.protocol_runs.create_index("started_at", expireAfterSeconds=90 * 86400)
        await self.db.memories.create_index([("owner_id", 1), ("created_at", -1)])
        await self.db.memories.create_index("expires_at", expireAfterSeconds=0, sparse=True)
        await self.db.conversations.create_index([("owner_id", 1), ("source", 1), ("timestamp", -1)])
        await self.db.conversations.create_index([("owner_id", 1), ("timestamp", -1)])
        await self.db.conversations.create_index([
            ("owner_id", 1),
            ("source", 1),
            ("role", 1),
            ("timestamp", -1),
            ("metadata.turn_id", -1),
        ])
        await self.db.conversations.create_index([("owner_id", 1), ("metadata.node_id", 1), ("source", 1), ("timestamp", -1)])
        await self.db.conversations.create_index([("owner_id", 1), ("metadata.instance_id", 1), ("timestamp", 1)])
        await self.db.conversations.create_index("embedding", sparse=True)
        await self.db.conversations.create_index("metadata.turn_id", sparse=True)
        await self.db.turn_runs.create_index([("owner_id", 1), ("turn_id", 1)], unique=True)
        await self.db.turn_runs.create_index([("owner_id", 1), ("completed_at", -1)])
        await self.db.turn_runs.create_index([("owner_id", 1), ("source", 1), ("node_id", 1), ("started_at", -1)])
        await self.db.turn_runs.create_index("expires_at", expireAfterSeconds=0)
        await self.db.automation_fired.create_index(
            [("rule_id", 1), ("item_id", 1)], unique=True
        )
        await self.db.automation_fired.create_index(
            "fired_at", expireAfterSeconds=86400
        )
        # --- Trigger collections ---
        await self.db.trigger_rules.create_index("id", unique=True)
        await self.db.trigger_rules.create_index([("owner_id", 1), ("enabled", 1)])
        await self.db.trigger_rules.create_index([("owner_id", 1), ("surface", 1), ("updated_at", -1)])
        await self.db.trigger_rules.create_index([("owner_id", 1), ("origin.kind", 1), ("enabled", 1)])
        await self.db.trigger_rules.create_index([("origin.kind", 1), ("origin.source", 1), ("enabled", 1)])
        await self.db.trigger_instances.create_index("id", unique=True)
        await self.db.trigger_instances.create_index([("status", 1), ("due_at", 1)])
        await self.db.trigger_instances.create_index(
            [("status", 1), ("action_snapshot.kind", 1), ("due_at", 1)]
        )
        await self.db.trigger_instances.create_index([("owner_id", 1), ("status", 1), ("due_at", -1)])
        await self.db.trigger_instances.create_index([("owner_id", 1), ("status", 1), ("updated_at", -1)])
        await self.db.trigger_instances.create_index([("owner_id", 1), ("updated_at", -1), ("id", -1)])
        await self.db.trigger_instances.create_index([("status", 1), ("next_retry_at", 1)])
        await self.db.trigger_instances.create_index([
            ("owner_id", 1),
            ("origin_snapshot.kind", 1),
            ("updated_at", -1),
            ("id", -1),
        ])
        await self.db.trigger_instances.create_index([("owner_id", 1), ("status", 1), ("next_retry_at", 1)])
        await self.db.trigger_instances.create_index([
            ("owner_id", 1),
            ("origin_snapshot.kind", 1),
            ("source_event.rule_id", 1),
            ("status", 1),
            ("updated_at", -1),
        ])
        await self.db.trigger_instances.create_index([("rule_id", 1), ("status", 1)], sparse=True)
        await self._ensure_trigger_dedup_index()

        await self.db.inbound_events.create_index("idempotency_key", unique=True)
        await self.db.inbound_events.create_index("id", unique=True)
        await self.db.inbound_events.create_index(
            [("status", 1), ("next_attempt_at", 1), ("received_at", 1)]
        )
        await self.db.inbound_events.create_index(
            [("status", 1), ("lease_until", 1)]
        )
        await self.db.inbound_events.create_index(
            "expires_at", expireAfterSeconds=0
        )
        await self.db.push_channels.create_index(
            [("source", 1), ("resource_id", 1)], unique=True
        )
        await self.db.external_trigger_credentials.create_index("source", unique=True)
        # SystemPulse (Phase 9b): 30-day retention for tick history
        await self.db.pulse_runs.create_index("tick_at", expireAfterSeconds=30 * 86400)
        await self.db.pulse_runs.create_index([("escalated", 1), ("tick_at", -1)])
        # Prefetch (Phase 9c): one doc per trigger/protocol; TTL cleans up stale
        # running/ready/failed docs a few minutes after their fire_time.
        await self.db.prefetched_results.create_index(
            [("source", 1), ("trigger_id", 1), ("protocol_name", 1)], unique=True
        )
        await self.db.prefetched_results.create_index(
            "expires_at", expireAfterSeconds=0
        )
        await self.db.watcher_cursors.create_index("source", unique=True)
        await self.db.background_tasks.create_index("task_id", unique=True)
        await self.db.background_tasks.create_index("owner_id")
        await self.db.background_tasks.create_index([("owner_id", 1), ("created_at", -1), ("task_id", -1)])
        await self.db.background_tasks.create_index("status")
        await self.db.background_tasks.create_index("trigger_ref", sparse=True)
        await self.db.background_tasks.create_index(
            "expires_at", expireAfterSeconds=0, sparse=True
        )
        await self.db.pending_inputs.create_index("input_id", unique=True)
        await self.db.pending_inputs.create_index([("owner_id", 1), ("status", 1), ("created_at", -1)])
        await self.db.pending_inputs.create_index([("source.type", 1), ("source.id", 1), ("status", 1)])
        await self.db.pinned_widgets.create_index([("owner_id", 1), ("widget_id", 1)], unique=True)
        # Habits V0: owner-scoped habit definitions plus append-only logs.
        await self.db.habits.create_index([("owner_id", 1), ("id", 1)], unique=True)
        await self.db.habits.create_index([("owner_id", 1), ("name_key", 1)], unique=True)
        await self.db.habits.create_index([("owner_id", 1), ("active", 1)])
        await self.db.habit_logs.create_index([("owner_id", 1), ("habit_id", 1), ("logged_at", -1)])
        await self.db.habit_checkin_plans.create_index([("owner_id", 1), ("id", 1)], unique=True)
        await self.db.habit_checkin_plans.create_index([("owner_id", 1), ("habit_id", 1), ("active", 1)])
        await self.db.habit_checkin_plans.create_index([("owner_id", 1), ("rule_id", 1)])
        await self.db.attention_state.create_index("owner_id", unique=True)
        await self.db.attention_schedules.create_index([("owner_id", 1), ("enabled", 1)])
        await self.db.attention_schedules.create_index([("owner_id", 1), ("name", 1)])
        await self.db.user_preferences.create_index("owner_id", unique=True)
        await self.db.ws_device_credentials.create_index("device_id", unique=True)
        await self.db.ws_device_credentials.create_index([("owner_id", 1), ("node_id", 1)])
        await self.db.ws_pairing_codes.create_index("code_hash", unique=True)
        await self.db.ws_pairing_codes.create_index("expires_at", expireAfterSeconds=0)
        await self.db.ws_tickets.create_index("ticket_hash", unique=True)
        await self.db.ws_tickets.create_index("expires_at", expireAfterSeconds=0)
        await self.db.ws_pairing_attempts.create_index("client_key", unique=True)
        await self.db.ws_pairing_attempts.create_index("last_attempt_at", expireAfterSeconds=3600)

    async def _ensure_trigger_dedup_index(self) -> None:
        """Ensure dedup keys are unique when present."""
        await self.db.trigger_instances.create_index(
            "dedup_key",
            name="trigger_instances_dedup_key_unique",
            unique=True,
            partialFilterExpression={"dedup_key": {"$type": "string"}},
        )

    async def disconnect(self) -> None:
        """Disconnect from MongoDB."""
        if self.client:
            self.client.close()
            self.client = None
            self.db = None
            logger.info("Disconnected from MongoDB")

    def get_collection(self, name: str) -> Any:
        if self.db is None:
            raise ConnectionError("Not connected to MongoDB")
        return self.db[name]

    async def health_check(self) -> bool:
        """Check if MongoDB is healthy."""
        try:
            if self.client is not None:
                await self.client.admin.command('ping')
                return True
            return False
        except Exception as e:
            logger.error(f"MongoDB health check failed: {e}")
            return False

    async def store_message(
        self,
        owner_id: str,
        role: str,
        content: Union[str, list[dict]],
        source: str = "user",
        metadata: Optional[Dict] = None,
    ) -> None:
        """Store a conversation message in the 'conversations' collection.

        Args:
            source: Origin of the turn -- "user" or "system".
                    Automations use "system" with origin metadata.
            metadata: Optional extra fields (e.g. automation_name, trigger).
        """
        try:
            collection = self.get_collection("conversations")
            doc: Dict[str, Any] = {
                "owner_id": owner_id,
                "role": role,
                "content": content,
                "source": source,
                "timestamp": datetime.now(timezone.utc),
            }
            if metadata:
                doc["metadata"] = metadata
            # Embed natural-language turns only — skip tool results
            meta = metadata or {}
            _is_tool_result = (
                meta.get("turn_type") == "tool_result"
                or (isinstance(content, str) and content.lstrip().startswith("<tool_result>"))
            )
            if isinstance(content, str) and role in ("user", "assistant") and len(content) > 20 and not _is_tool_result:
                try:
                    from services.embeddings import embedding_service
                    doc["embedding"] = embedding_service.embed_one(content[:512])
                except Exception as embed_err:
                    logger.warning(f"Embedding failed for message (non-fatal): {embed_err}")
            await collection.insert_one(doc)
            logger.debug(f"Stored {role} message for owner {owner_id} (source={source})")
        except Exception as e:
            logger.error(f"Error storing message: {e}")
            raise

    async def upsert_user_turn(
        self,
        owner_id: str,
        turn_id: str,
        content: Union[str, list[dict]],
        *,
        source: str = "user",
        metadata: Optional[Dict] = None,
    ) -> None:
        """Persist accepted user input early, idempotently keyed by turn_id."""
        try:
            collection = self.get_collection("conversations")
            meta = dict(metadata or {})
            meta["turn_id"] = turn_id
            meta.setdefault("turn_status", "pending")
            await collection.update_one(
                {"owner_id": owner_id, "role": "user", "metadata.turn_id": turn_id},
                {
                    "$set": {
                        "content": content,
                        "source": source,
                        "metadata": meta,
                    },
                    "$setOnInsert": {
                        "owner_id": owner_id,
                        "role": "user",
                        "timestamp": datetime.now(timezone.utc),
                    },
                },
                upsert=True,
            )
        except Exception as e:
            logger.error("Error upserting user turn: %s", e)
            raise

    async def get_last_spoken_response(self, owner_id: str, *, limit: int = 20) -> Optional[str]:
        """Return the latest completed user-turn assistant text suitable for repeat_last.

        Matches the previous in-memory writer: completed user-source turns only,
        assistant text_only rows that were not interrupted/suppressed/empty.
        Proactive system deliveries are excluded.
        """
        try:
            collection = self.get_collection("conversations")
            cursor = collection.find(
                {
                    "owner_id": owner_id,
                    "source": "user",
                    "role": "assistant",
                    "metadata.turn_type": "text_only",
                    "metadata.interrupted": {"$ne": True},
                    "metadata.delivery": {"$nin": ["suppressed", "silent"]},
                },
                {"content": 1, "metadata.turn_id": 1},
            ).sort("timestamp", -1).limit(limit)
            candidates = await cursor.to_list(length=limit)
            for row in candidates:
                content = row.get("content")
                if not isinstance(content, str):
                    continue
                text = content.strip()
                if not text:
                    continue
                turn_id = (row.get("metadata") or {}).get("turn_id")
                if not turn_id:
                    continue
                user_row = await collection.find_one(
                    {
                        "owner_id": owner_id,
                        "role": "user",
                        "source": "user",
                        "metadata.turn_id": turn_id,
                        "metadata.turn_status": "completed",
                    },
                    {"_id": 1},
                )
                if user_row:
                    return text
            return None
        except Exception as e:
            logger.warning("Error loading last spoken response: %s", e)
            return None

    async def mark_user_turn_status(
        self,
        owner_id: str,
        turn_id: str,
        status: str,
        *,
        delivery: Optional[str] = None,
    ) -> None:
        """Update durable user-turn lifecycle metadata."""
        try:
            collection = self.get_collection("conversations")
            update: Dict[str, Any] = {"metadata.turn_status": status}
            if delivery is not None:
                update["metadata.delivery"] = delivery
            await collection.update_one(
                {"owner_id": owner_id, "role": "user", "metadata.turn_id": turn_id},
                {"$set": update},
            )
        except Exception as e:
            logger.warning("Error marking user turn status: %s", e)
            raise

    async def get_history(
        self,
        owner_id: str,
        limit: int = 20,
        source_filter: Optional[List[str]] = None,
        include_timestamps: bool = False,
        skip_tool_results: bool = False,
        include_metadata: bool = False,
        exclude_deliveries: Optional[List[str]] = None,
        include_deliveries: Optional[List[str]] = None,
        exclude_turn_id: Optional[str] = None,
        exclude_turn_types: Optional[List[str]] = None,
        node_id: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Dict]:
        """Retrieve recent chat history for a user in chronological order.

        Args:
            source_filter: If set, only return messages with matching source values.
                           e.g. ["user"] to exclude system/automation noise.
            include_timestamps: If True, include ISO-8601 timestamp in each message.
            skip_tool_results: If True, exclude tool result messages
                               (turn_type=tool_result, or legacy XML-wrapped rows).
            include_metadata: If True, include the metadata dict on each message when present.
            exclude_deliveries: If set, exclude rows whose `metadata.delivery` is
                                in the list (rows missing the field are kept —
                                user turns and pre-delivery-tag rows). Used by
                                the LLM system tail to drop silent / suppressed
                                trigger traces; suppressed user-source turns
                                stay in LLM context.
            include_deliveries: If set, only return rows whose `metadata.delivery`
                                is in the list. Used by the headless audit feed.
                                Mutually exclusive with `exclude_deliveries`.
            exclude_turn_id: If set, omit rows for the in-flight turn already
                             represented by the live user input.
            exclude_turn_types: If set, omit rows whose `metadata.turn_type` is in the list.
            since: If set, only include messages at or after this UTC timestamp.
        """
        if exclude_deliveries and include_deliveries:
            raise ValueError(
                "get_history: pass either exclude_deliveries or include_deliveries, not both"
            )
        try:
            collection = self.get_collection("conversations")
            query: Dict[str, Any] = {"owner_id": owner_id}
            if source_filter:
                query["source"] = {"$in": source_filter}
            if skip_tool_results:
                query["$nor"] = [
                    {"metadata.turn_type": "tool_result"},
                    {"content": {"$regex": r"^\s*<tool_result>"}},
                ]
            if exclude_deliveries:
                query["metadata.delivery"] = {"$nin": list(exclude_deliveries)}
            elif include_deliveries:
                # `$in` does not match missing fields — exactly the semantic we want
                # for the audit feed (only delivery-tagged rows surface).
                query["metadata.delivery"] = {"$in": list(include_deliveries)}
            if exclude_turn_id:
                query["metadata.turn_id"] = {"$ne": exclude_turn_id}
            if exclude_turn_types:
                query["metadata.turn_type"] = {"$nin": list(exclude_turn_types)}
            if node_id:
                query["metadata.node_id"] = node_id
            if since:
                query["timestamp"] = {"$gte": since}
            cursor = collection.find(query).sort("timestamp", -1).limit(limit)
            messages = await cursor.to_list(length=limit)
            result = []
            for m in reversed(messages):
                row: Dict[str, Any] = {"role": m["role"], "content": m["content"]}
                if include_timestamps:
                    row["timestamp"] = m["timestamp"].isoformat()
                if include_metadata and m.get("metadata"):
                    row["metadata"] = m["metadata"]
                result.append(row)
            return result
        except Exception as e:
            logger.error(f"Error getting history: {e}")
            return []

    async def resolve_conversation_window_start(
        self,
        owner_id: str,
        node_id: Optional[str],
        *,
        gap: timedelta,
        now: Optional[datetime] = None,
        limit: int = 200,
        exclude_turn_id: Optional[str] = None,
        visible_deliveries: Optional[List[str]] = None,
    ) -> Optional[datetime]:
        """Return the start timestamp for the current node-local prompt window.

        Long-term storage remains owner-wide. This only decides how much already
        node-scoped short-term history should be injected into the next prompt.
        """
        if gap.total_seconds() <= 0:
            return None

        now = now or datetime.now(timezone.utc)
        try:
            collection = self.get_collection("conversations")
            visible_delivery_values = list(visible_deliveries or [])
            query: Dict[str, Any] = {
                "owner_id": owner_id,
                "timestamp": {"$lte": now},
                "$or": [
                    {"source": "user"},
                    {
                        "source": "system",
                        "role": "assistant",
                        "metadata.delivery": {"$in": visible_delivery_values},
                    },
                ],
            }
            if node_id:
                query["metadata.node_id"] = node_id
            if exclude_turn_id:
                query["metadata.turn_id"] = {"$ne": exclude_turn_id}

            cursor = collection.find(query, {"timestamp": 1}).sort("timestamp", -1).limit(limit)
            docs = await cursor.to_list(length=limit)
        except Exception as e:
            logger.warning("Error resolving conversation window start: %s", e)
            return now

        timestamps = [
            doc.get("timestamp")
            for doc in docs
            if isinstance(doc.get("timestamp"), datetime)
        ]
        if not timestamps:
            return now

        newest = timestamps[0]
        if now - newest > gap:
            return now

        window_start = newest
        newer = newest
        for older in timestamps[1:]:
            if newer - older > gap:
                break
            window_start = older
            newer = older
        return window_start

    async def backfill_conversation_embeddings(self, owner_id: str, limit: int = 50) -> int:
        """Embed conversation messages that are missing embeddings.

        Called as a background task after context compaction drops messages,
        ensuring they remain searchable via recall().
        Returns the number of messages backfilled.
        """
        try:
            from services.embeddings import embedding_service

            collection = self.get_collection("conversations")
            docs = await collection.find(
                {
                    "owner_id": owner_id,
                    "embedding": {"$exists": False},
                    "role": {"$in": ["user", "assistant"]},
                    "content": {"$not": {"$regex": r"^\s*<tool_result>"}},
                },
                {"_id": 1, "content": 1},
            ).sort("timestamp", -1).limit(limit).to_list(length=limit)

            if not docs:
                return 0

            # Extract text and filter out short/empty messages
            texts: list[tuple[Any, str]] = []
            for doc in docs:
                content = extract_text_content(doc.get("content", ""))
                if len(content) >= 20:
                    texts.append((doc["_id"], content[:512]))

            if not texts:
                return 0

            # Batch embed for efficiency
            vectors = embedding_service.embed([t for _, t in texts])

            count = 0
            for (doc_id, _), vec in zip(texts, vectors):
                try:
                    await collection.update_one(
                        {"_id": doc_id},
                        {"$set": {"embedding": vec}},
                    )
                    count += 1
                except Exception as e:
                    logger.warning("Embedding backfill failed for doc %s: %s", doc_id, e)

            if count:
                logger.info("Backfilled embeddings for %d conversation messages (owner=%s)", count, owner_id)
            return count
        except Exception as e:
            logger.error("Error in backfill_conversation_embeddings: %s", e)
            return 0

    async def clear_conversation_history(self, owner_id: str) -> int:
        """Delete all conversation history for an owner. Returns count of deleted messages."""
        try:
            collection = self.get_collection("conversations")
            result = await collection.delete_many({"owner_id": owner_id})
            logger.info(f"Cleared {result.deleted_count} messages for owner {owner_id}")
            return result.deleted_count
        except Exception as e:
            logger.error(f"Error clearing conversation history: {e}")
            raise

    async def store_turn_run(self, summary: Dict[str, Any]) -> None:
        """Upsert compact operational telemetry for one turn.

        This collection intentionally stores timings and metadata only. User-visible
        content remains in `conversations`, linked by `turn_id`.
        """
        try:
            owner_id = summary.get("owner_id")
            turn_id = summary.get("turn_id")
            if not owner_id or not turn_id:
                return

            allowed = {
                "turn_id",
                "owner_id",
                "connection_id",
                "node_id",
                "node_label",
                "location_ref",
                "source",
                "modality",
                "delivery",
                "origin",
                "status",
                "started_at",
                "completed_at",
                "response_ms",
                "total_ms",
                "stages",
                "stt",
                "turn_detection",
                "voice",
                "tool_routing",
                "model",
                "reasoning_effort",
                "reasoning_chars",
                "expires_at",
            }
            doc = {key: value for key, value in summary.items() if key in allowed}
            collection = self.get_collection("turn_runs")
            await collection.update_one(
                {"owner_id": owner_id, "turn_id": turn_id},
                {"$set": doc},
                upsert=True,
            )
        except Exception as e:
            logger.warning("Error storing turn run telemetry: %s", e)
            raise

    async def store_tool_data(self, owner_id: str, tool_name: str, data: Dict) -> None:
        """Upsert tool-specific data in the 'tool_data' collection."""
        try:
            collection = self.get_collection("tool_data")
            await collection.update_one(
                {"owner_id": owner_id, "tool": tool_name},
                {"$set": {"data": data, "updated_at": datetime.now(timezone.utc)}},
                upsert=True
            )
            logger.debug(f"Stored tool data for {tool_name} (Owner: {owner_id})")
        except Exception as e:
            logger.error(f"Error storing tool data: {e}")
            raise

    async def get_tool_data(self, owner_id: str, tool_name: str) -> Dict:
        """Retrieve tool-specific data for an owner."""
        try:
            collection = self.get_collection("tool_data")
            result = await collection.find_one({"owner_id": owner_id, "tool": tool_name})
            return result["data"] if result else {}
        except Exception as e:
            logger.error(f"Error getting tool data: {e}")
            return {}

    async def delete_tool_data(self, owner_id: str, tool_name: str) -> None:
        """Delete tool-specific data for an owner."""
        try:
            collection = self.get_collection("tool_data")
            await collection.delete_one({"owner_id": owner_id, "tool": tool_name})
            logger.info(f"Deleted tool data for {tool_name} (Owner: {owner_id})")
        except Exception as e:
            logger.error(f"Error deleting tool data: {e}")
            raise

# Create global database service instance
mongodb: MongoDBService = MongoDBService() 