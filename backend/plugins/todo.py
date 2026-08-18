"""
Todo Plugin for Jarvis AI Assistant.
"""

from typing import Optional
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from core.context import get_ctx
from core.decorators import tool
from core.id import generate_id
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.consent import require_consent
from core.plugins.result import ToolResult
from core.plugins.ui import push_ui, receipt_envelope
from core.plugins.types import JarvisPlugin, PluginMetadata, UIEnvelope, WidgetLayout, WidgetSize
from plugins import db


class TodoItem(BaseModel):
    id: str = Field(default_factory=generate_id)
    text: str
    completed: bool = False
    created_at: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp()))


class TodoPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="todo",
        version="2.1.0",
        description="Simple task management with server-driven UI.",
        utterances=[
            "add something to my task list",
            "add this to my todo list",
            "show me my tasks",
            "what is on my todo list",
            "mark that task as done",
            "mark off a task",
            "check off my todo",
            "delete the task about groceries",
            "clear completed tasks",
            "show my task list on screen",
        ],
    )

    async def _todo_envelope(self, items: list[TodoItem], expires_in: Optional[int] = None) -> UIEnvelope:
        """Pure envelope construction — no side effects."""
        total = len(items)
        completed = sum(1 for item in items if item.completed)
        progress = (completed / total) if total else 0.0

        expires_at = None
        if expires_in:
            expires_at = int((datetime.now(timezone.utc).timestamp() + expires_in) * 1000)

        title = "Inbox"
        return UIEnvelope(
            widget_id="todo-inbox",
            component="TodoWidget",
            title=title,
            expires_at=expires_at,
            layout=WidgetLayout(size=WidgetSize.TALL, priority=8),
            data={
                "title": title,
                "items": [item.model_dump(mode="json") for item in items],
                "progress": progress,
            },
        )

    @tool
    async def get_tasks(self) -> list[TodoItem]:
        """
        List all tasks. The .id is required for complete_task() / update_task() / delete_task().
        Use toggle_task() only to reopen. Never read .id aloud.
        """
        items = await db.load_models("todo", TodoItem)
        push_ui(await self._todo_envelope(items))
        return items

    @tool
    async def add_task(self, text: str) -> ToolResult:
        """Add a new task."""
        items = await db.load_models("todo", TodoItem)
        items.append(TodoItem(text=text))
        await db.save_models("todo", items)
        content = f"Added '{text}'."
        return ToolResult(
            content=content,
            ui=[
                await self._todo_envelope(items)
                if _should_return_widget()
                else _todo_receipt("Task Added", text, f"{len(items)} total")
            ],
        )

    @tool
    async def complete_task(self, task_id: str) -> ToolResult | CapabilityErrorDetail:
        """
        Mark a task complete. Idempotent if already done. Call get_tasks() first for task_id.
        """
        items = await db.load_models("todo", TodoItem)
        for i, item in enumerate(items):
            if item.id == task_id:
                if item.completed:
                    return ToolResult(
                        content=f"Already completed '{item.text}'.",
                        ui=[
                            await self._todo_envelope(items)
                            if _should_return_widget()
                            else _todo_receipt("Task Completed", item.text)
                        ],
                    )
                items[i] = item.model_copy(update={"completed": True})
                await db.save_models("todo", items)
                return ToolResult(
                    content=f"Completed '{item.text}'.",
                    ui=[
                        await self._todo_envelope(items)
                        if _should_return_widget()
                        else _todo_receipt("Task Completed", item.text)
                    ],
                )
        return _task_not_found(task_id)

    @tool
    async def toggle_task(self, task_id: str) -> ToolResult | CapabilityErrorDetail:
        """
        Flip complete ↔ reopen. Prefer complete_task() to mark done. Call get_tasks() first.
        """
        items = await db.load_models("todo", TodoItem)
        for i, item in enumerate(items):
            if item.id == task_id:
                completed = not item.completed
                items[i] = item.model_copy(update={"completed": completed})
                await db.save_models("todo", items)
                state = "Completed" if completed else "Reopened"
                return ToolResult(
                    content=f"{state} '{item.text}'.",
                    ui=[
                        await self._todo_envelope(items)
                        if _should_return_widget()
                        else _todo_receipt(f"Task {state}", item.text)
                    ],
                )
        return _task_not_found(task_id)

    @tool
    async def update_task(self, task_id: str, new_text: str) -> ToolResult | CapabilityErrorDetail:
        """
        Edit the text of an existing task. Call get_tasks() first to get the task_id.
        Use this to rename/correct a task — never toggle+delete+recreate just to edit text.
        """
        items = await db.load_models("todo", TodoItem)
        for i, item in enumerate(items):
            if item.id == task_id:
                items[i] = item.model_copy(update={"text": new_text})
                await db.save_models("todo", items)
                content = f"Updated task to '{new_text}'."
                return ToolResult(
                    content=content,
                    ui=[
                        await self._todo_envelope(items)
                        if _should_return_widget()
                        else _todo_receipt("Task Updated", new_text, f"Was: {item.text}")
                    ],
                )
        return _task_not_found(task_id)

    @tool
    async def delete_task(self, task_id: str) -> ToolResult | CapabilityErrorDetail:
        """
        Permanently delete a single task by id. Destructive — prefer complete_task
        when the user just wants to mark done. Call get_tasks() first to
        resolve the task_id.
        """
        items = await db.load_models("todo", TodoItem)
        filtered = [item for item in items if item.id != task_id]
        if len(filtered) == len(items):
            return _task_not_found(task_id)
        deleted = next(item for item in items if item.id == task_id)
        await db.save_models("todo", filtered)
        return ToolResult(
            content=f"Deleted task '{deleted.text}'.",
            ui=[
                await self._todo_envelope(filtered)
                if _should_return_widget()
                else _todo_receipt("Task Deleted", deleted.text, f"{len(filtered)} remaining")
            ],
        )

    @tool
    async def clear_tasks(self) -> ToolResult | CapabilityErrorDetail:
        """Permanently delete all tasks. Has built-in approval — call immediately."""
        items = await db.load_models("todo", TodoItem)
        if not items:
            return ToolResult(content="Task list is already empty.")

        async def _do_clear() -> ToolResult:
            await db.save_models("todo", [])
            return ToolResult(content=f"Cleared {len(items)} tasks.")

        detail = "\n".join(f"- {item.text}" for item in items[:20])
        if len(items) > 20:
            detail += f"\n- …and {len(items) - 20} more"
        return await require_consent(
            f"Permanently delete all {len(items)} tasks?",
            _do_clear,
            detail=detail,
        )


def _todo_receipt(title: str, line: str, sublabel: str | None = None) -> UIEnvelope:
    return receipt_envelope(title, line, sublabel=sublabel)


def _task_not_found(task_id: str) -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code="not_found", message=f"Task {task_id!r} not found.")


def _should_return_widget() -> bool:
    return get_ctx().get("invocation_source") == "ui_action"
