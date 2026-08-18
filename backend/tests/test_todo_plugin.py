import pytest

from plugins import todo
from plugins.todo import TodoItem, TodoPlugin


@pytest.mark.asyncio
async def test_get_tasks_attaches_widget_and_returns_items(monkeypatch):
    items = [
        TodoItem(id="done-1", text="Already finished", completed=True),
        TodoItem(id="todo-1", text="Do this next", completed=False),
        TodoItem(id="done-2", text="Also finished", completed=True),
    ]
    plugin = TodoPlugin()

    async def load_items(*_args, **_kwargs):
        return items

    monkeypatch.setattr(todo.db, "load_models", load_items)

    result = await plugin.get_tasks()
    envelope = await plugin._todo_envelope(items)

    assert [item.id for item in result] == ["done-1", "todo-1", "done-2"]
    assert envelope.data["progress"] == pytest.approx(2 / 3)
    assert envelope.component == "TodoWidget"


@pytest.mark.asyncio
async def test_todo_mutations_use_receipts_by_default(monkeypatch, invoke_tool):
    saved = []
    plugin = TodoPlugin()

    async def load_items(*_args, **_kwargs):
        return []

    async def save_items(_name, items):
        saved[:] = items

    monkeypatch.setattr(todo.db, "load_models", load_items)
    monkeypatch.setattr(todo.db, "save_models", save_items)

    result = await invoke_tool(plugin, "add_task", "Review receipts")

    assert result.content == "Added 'Review receipts'."
    assert result.ui[0].component == "ContentWidget"
    assert result.ui[0].data["display"] == "receipt"
    assert saved[0].text == "Review receipts"


@pytest.mark.asyncio
async def test_todo_mutations_return_widget_for_ui_actions(monkeypatch, tool_context, invoke_tool):
    items = [TodoItem(id="todo-1", text="Review receipts", completed=False)]
    plugin = TodoPlugin()

    async def load_items(*_args, **_kwargs):
        return items

    async def save_items(_name, updated):
        items[:] = updated

    monkeypatch.setattr(todo.db, "load_models", load_items)
    monkeypatch.setattr(todo.db, "save_models", save_items)

    with tool_context(extras={"invocation_source": "ui_action"}):
        result = await invoke_tool(plugin, "toggle_task", "todo-1")

    assert result.content == "Completed 'Review receipts'."
    assert result.ui[0].component == "TodoWidget"
    assert result.ui[0].data["items"][0]["completed"] is True


@pytest.mark.asyncio
async def test_complete_task_marks_incomplete(monkeypatch, invoke_tool):
    items = [TodoItem(id="todo-1", text="Send email", completed=False)]
    plugin = TodoPlugin()

    async def load_items(*_args, **_kwargs):
        return items

    async def save_items(_name, updated):
        items[:] = updated

    monkeypatch.setattr(todo.db, "load_models", load_items)
    monkeypatch.setattr(todo.db, "save_models", save_items)

    result = await invoke_tool(plugin, "complete_task", "todo-1")

    assert result.content == "Completed 'Send email'."
    assert items[0].completed is True


@pytest.mark.asyncio
async def test_complete_task_idempotent_when_already_done(monkeypatch, invoke_tool):
    items = [TodoItem(id="todo-1", text="Send email", completed=True)]
    plugin = TodoPlugin()

    async def load_items(*_args, **_kwargs):
        return items

    monkeypatch.setattr(todo.db, "load_models", load_items)

    result = await invoke_tool(plugin, "complete_task", "todo-1")

    assert result.content == "Already completed 'Send email'."
    assert items[0].completed is True


@pytest.mark.asyncio
async def test_todo_stale_id_returns_actionable_error(monkeypatch, invoke_tool):
    async def load_items(*_args, **_kwargs):
        return [TodoItem(id="todo-1", text="Send email", completed=False)]

    monkeypatch.setattr(todo.db, "load_models", load_items)

    result = await invoke_tool(TodoPlugin(), "complete_task", "missing")

    assert result.code == "not_found"
    assert result.message == "Task 'missing' not found."


@pytest.mark.asyncio
async def test_clear_tasks_requires_consent_before_write(monkeypatch, invoke_tool):
    items = [TodoItem(id="todo-1", text="Send email")]
    save_calls = []
    captured = {}

    async def load_items(*_args, **_kwargs):
        return items

    async def save_items(_name, updated):
        save_calls.append(updated)

    async def fake_require_consent(description, action, detail=""):
        captured["description"] = description
        captured["detail"] = detail
        assert save_calls == []
        return await action()

    monkeypatch.setattr(todo.db, "load_models", load_items)
    monkeypatch.setattr(todo.db, "save_models", save_items)
    monkeypatch.setattr(todo, "require_consent", fake_require_consent)

    result = await invoke_tool(TodoPlugin(), "clear_tasks")

    assert result.content == "Cleared 1 tasks."
    assert captured["description"] == "Permanently delete all 1 tasks?"
    assert "Send email" in captured["detail"]
    assert save_calls == [[]]
