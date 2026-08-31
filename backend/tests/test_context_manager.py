import pytest

from core.context_manager import compact_history


@pytest.mark.asyncio
async def test_dropped_history_backfills_embeddings_by_owner_id(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(
        "core.context_manager._compute_history_budget",
        lambda _prompt: 20,
    )
    monkeypatch.setattr(
        "core.context_manager._message_tokens",
        lambda _msg: 12,
    )
    monkeypatch.setattr(
        "core.context_manager._schedule_embedding_backfill",
        captured.append,
    )

    history = [
        {"role": "user", "content": "first request that will be dropped"},
        {"role": "assistant", "content": "first reply that will be dropped"},
        {"role": "user", "content": "latest request that should remain"},
    ]

    kept, stats = await compact_history(
        history,
        system_prompt="static",
        owner_id="owner-geoff",
    )

    assert stats["messages_dropped"] > 0
    assert kept
    assert captured == ["owner-geoff"]
