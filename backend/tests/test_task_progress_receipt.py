from datetime import datetime, timezone

from plugins.agents.client import task_progress_receipt_envelope


def test_task_progress_receipt_running():
    envelope = task_progress_receipt_envelope(
        "task-1",
        {
            "status": "running",
            "title": "2713 review",
            "mode": "jarvis",
            "live_status": "Checking calendar…",
            "created_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
        },
    )

    assert envelope.widget_id == "task-receipt-task-1"
    assert envelope.data["receipt_kind"] == "task_progress"
    assert envelope.data["ref_id"] == "task-1"
    assert envelope.data["title"] == "2713 review"
    assert envelope.data["line"] == "Checking calendar…"
    assert envelope.data["action"] == {"type": "open_background_task", "task_id": "task-1"}
    assert envelope.expires_at is None


def test_task_progress_receipt_terminal_sets_ttl():
    envelope = task_progress_receipt_envelope(
        "task-2",
        {
            "status": "completed",
            "title": "2713 review",
            "mode": "code",
            "result": "Updated README",
            "created_at": 1_700_000_000_000,
        },
    )

    assert envelope.data["title"] == "2713 review"
    assert envelope.data["status"] == "completed"
    assert envelope.expires_at is not None


def test_task_progress_receipt_cancelled_is_terminal():
    envelope = task_progress_receipt_envelope(
        "task-4",
        {
            "status": "cancelled",
            "title": "2713 review",
            "result": "Task was cancelled.",
            "created_at": 1_700_000_000_000,
        },
    )

    assert envelope.data["status"] == "cancelled"
    assert envelope.expires_at is not None


def test_task_progress_receipt_approval_prefers_pending_widget_action():
    envelope = task_progress_receipt_envelope(
        "task-3",
        {
            "status": "running",
            "attention": "approval",
            "pending_input": {
                "prompt": "Approve sending this email?",
                "widget_id": "pending-inp-1",
            },
        },
    )

    assert envelope.data["title"] == "Needs approval"
    assert envelope.data["action"] == {
        "type": "activate_widget",
        "widget_id": "pending-inp-1",
        "task_id": "task-3",
    }
