"""
Protocol context builder for system-initiated turns.

Formats protocol steps, metadata, and execution rules into a context string
that the orchestrator injects as the user message for protocol runs.
"""

import re
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo

from services.database.mongodb import mongodb

EXECUTION_RULES = (
    "EXECUTION RULES:\n"
    "- Execute independent steps together. Wait for a result only when a later step needs it.\n"
    "- Do NOT speak before or between tool calls.\n"
    "- After gathering all results, deliver ONE cohesive spoken response synthesizing everything.\n"
    "- If a tool is unavailable or fails, skip it and briefly note what was skipped at the end.\n"
    "- Keep the delivery natural and conversational."
)


async def build_protocol_context(
    protocol_name: str, owner_id: str, tz_name: str = "UTC"
) -> str:
    """
    Load a protocol from the database and return a formatted context string
    with steps, run metadata, and execution rules.

    Returns empty string if the protocol is not found.
    """
    doc = await mongodb.db.protocols.find_one(
        {"name": {"$regex": f"^{re.escape(protocol_name)}$", "$options": "i"}, "owner_id": owner_id}
    )
    if not doc:
        return ""

    steps = "\n".join(f"{i+1}. {s}" for i, s in enumerate(doc["steps"]))

    # Build metadata line from protocol doc
    meta_parts = []
    if doc.get("description"):
        meta_parts.append(f'Description: {doc["description"]}')

    run_count = doc.get("run_count", 0)
    last_run: Optional[datetime] = doc.get("last_run_at")
    if run_count > 0 and last_run:
        try:
            tz = ZoneInfo(tz_name)
            local_last = last_run.astimezone(tz)
            last_str = local_last.strftime("%A at %-I:%M %p")
        except Exception:
            last_str = last_run.isoformat()
        meta_parts.append(f"Run count: {run_count} | Last run: {last_str}")

    meta = "\n".join(meta_parts)
    meta_block = f"\n{meta}" if meta else ""

    directive = doc.get("directive")
    directive_block = f"\nDirective: {directive}" if directive else ""

    return (
        f'\nPROTOCOL "{protocol_name}":{meta_block}{directive_block}\n'
        f'Steps:\n{steps}\n\n{EXECUTION_RULES}'
    )


async def is_protocol_prefetch_safe(protocol_name: str, owner_id: str) -> bool:
    """Return True iff the protocol is explicitly flagged as prefetch-safe.

    Defaults to False (and missing protocols are False too) so prefetch never
    runs steps with side effects ahead of fire time. The flag is set on the
    protocol document via `create_protocol(prefetch_safe=True)`.
    """
    doc = await mongodb.db.protocols.find_one(
        {"name": {"$regex": f"^{re.escape(protocol_name)}$", "$options": "i"}, "owner_id": owner_id},
        projection={"prefetch_safe": 1},
    )
    return bool(doc and doc.get("prefetch_safe"))
