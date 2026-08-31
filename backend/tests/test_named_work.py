import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.agents.work import (
    format_roster,
    infer_title,
    inspect_ide_argv,
    inspect_launch_argv,
    inspect_macos_open_argv,
    latest_per_work_id,
    match_open_work,
    path_under_cwd,
    recency_roster,
    resolve_folder,
    resolve_from_docs,
)


def _doc(**overrides):
    base = {
        "task_id": "task-a",
        "work_id": "work-a",
        "title": "2713 review",
        "ref": "#2713",
        "open": True,
        "status": "completed",
        "cwd": "/Users/geoff/dev/aetheron-connect-v2",
        "progress_summary": "Looked at review comments",
        "result": "Smallest fix is the judge gate.",
        "created_at": datetime(2026, 8, 19, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


CONNECT = _doc()
CONNECT_RESUME = _doc(
    task_id="task-a2",
    status="running",
    created_at=datetime(2026, 8, 19, 1, tzinfo=timezone.utc),
)
MICHELL = _doc(
    task_id="task-b",
    work_id="work-b",
    title="Quoting ports",
    ref="ports",
    cwd="/Users/geoff/dev/michell-wool-ai-role-discovery",
    status="completed",
    progress_summary="Need an override for shipping port",
    result="Port override is missing.",
)


def test_latest_run_is_per_lineage_not_global():
    latest = latest_per_work_id([CONNECT_RESUME, CONNECT, MICHELL])
    assert [d["task_id"] for d in latest] == ["task-a2", "task-b"]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("task-a", "task-a"),
        ("work-b", "task-b"),
        ("2713 review", "task-a"),
        ("the 2713 PR", "task-a"),
        ("#2713", "task-a"),
        ("quoting ports", "task-b"),
        ("michell-wool-ai-role-discovery", "task-b"),
        ("connect-v2", "task-a"),
    ],
)
def test_resolve_unique_title_ref_cwd(target, expected):
    resolved = resolve_from_docs([CONNECT, MICHELL], target)
    assert resolved.status == "single"
    assert resolved.doc["task_id"] == expected


def test_resolve_ambiguous_does_not_guess_latest_across_repos():
    left = _doc(title="Review comments", cwd="/tmp/shop")
    right = _doc(
        task_id="task-b",
        work_id="work-b",
        title="Review quoting",
        cwd="/tmp/wool",
    )
    resolved = resolve_from_docs([left, right], "review")
    assert resolved.status == "ambiguous"
    assert {d["work_id"] for d in resolved.candidates} == {"work-a", "work-b"}


def test_resolve_ignores_closed():
    closed = _doc(open=False, title="2713 review")
    resolved = resolve_from_docs([closed, MICHELL], "2713")
    assert resolved.status == "none"


def test_generic_steer_asks_which_when_two_open():
    tweak = resolve_from_docs([CONNECT, MICHELL], "tweak it")
    assert tweak.status == "ambiguous"
    assert {d["work_id"] for d in tweak.candidates} == {"work-a", "work-b"}


def test_generic_steer_resolves_when_only_one_open():
    tweak = resolve_from_docs([CONNECT], "tweak it")
    assert tweak.status == "single"
    assert tweak.doc["task_id"] == "task-a"


def test_resolve_folder_nickname_and_infer_title(tmp_path):
    connect = tmp_path / "aetheron-connect-v2"
    wool = tmp_path / "michell-wool-ai-role-discovery"
    connect.mkdir()
    wool.mkdir()
    known = [str(connect), str(wool)]
    assert resolve_folder("connect-v2", known).path == str(connect)
    assert resolve_folder("michell wool", known).path == str(wool)
    assert resolve_folder("review", known).status == "none"
    assert infer_title(None, str(connect), "look at the 2713 review comments") == "2713 review"
    assert infer_title(None, str(wool), "check ports") == "michell-wool-ai-role-discovery"


def test_match_open_work_continue_vs_new_kickoff():
    steer = match_open_work(
        [CONNECT, MICHELL],
        prompt="Don't overcomplicate it — just the core of the review comments.",
    )
    assert steer.status == "ambiguous"
    kickoff = match_open_work(
        [CONNECT],
        prompt="In michell wool, check whether weekly quoting can override a client's shipping port.",
    )
    assert kickoff.status == "none"
    same = match_open_work([CONNECT], prompt="In connect-v2 look at the 2713 review.")
    assert same.status == "single"
    assert same.doc["task_id"] == "task-a"


def test_roster_includes_project_folders():
    text = format_roster([CONNECT], ["/Users/geoff/dev/aetheron-connect-v2"])
    assert "[OPEN WORK]" in text
    assert "[PROJECT FOLDERS]" in text


def test_roster_omits_empty_and_lists_titles_only():
    assert format_roster([]) == ""
    text = format_roster([CONNECT, MICHELL])
    assert text.startswith("[OPEN WORK]")
    assert "2713 review" in text
    assert "Quoting ports" in text
    assert "aetheron-connect-v2" in text
    assert "Do not recall()" in text
    assert "inspect to read" in text
    assert "close forgets" in text
    assert "House talk" in text
    assert CONNECT["result"] not in text


def test_roster_is_recency_not_all_open():
    oldest = _doc(
        task_id="task-old",
        work_id="work-old",
        title="Ancient ports",
        cwd="/tmp/old",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    extras = [
        _doc(
            task_id=f"task-{i}",
            work_id=f"work-{i}",
            title=f"Extra {i}",
            cwd=f"/tmp/extra-{i}",
            created_at=datetime(2026, 8, i + 1, tzinfo=timezone.utc),
        )
        for i in range(4)
    ]
    docs = [oldest, *extras, CONNECT]
    roster = recency_roster(docs)
    assert [d["title"] for d in roster] == [
        "2713 review",
        "Extra 3",
        "Extra 2",
        "Extra 1",
        "Extra 0",
    ]
    text = format_roster(docs)
    assert "2713 review" in text
    assert "Ancient ports" not in text
    found = resolve_from_docs(docs, "Ancient ports")
    assert found.status == "single"
    assert found.doc["work_id"] == "work-old"
    tweak = resolve_from_docs(docs, "tweak it")
    assert tweak.status == "ambiguous"
    assert len(tweak.candidates) == 5
    assert all(d["title"] != "Ancient ports" for d in tweak.candidates)


def test_inspect_launch_argv_quotes_cwd():
    argv = inspect_launch_argv("/tmp/my project", "sess-1", binary="claude")
    assert argv[0] == "osascript"
    script = argv[2]
    assert "cd '/tmp/my project'" in script or 'cd "/tmp/my project"' in script
    assert "claude --resume sess-1" in script


def test_path_under_cwd_rejects_escape(tmp_path):
    cwd = str(tmp_path)
    inside = tmp_path / "docs" / "plan.md"
    assert path_under_cwd("docs/plan.md", cwd) == str(inside.resolve())
    assert path_under_cwd(str(inside), cwd) == str(inside.resolve())
    assert path_under_cwd("../secret.md", cwd) is None


def test_inspect_ide_argv_reuses_window():
    argv = inspect_ide_argv("cursor", ["/tmp/project", "/tmp/project/docs/plan.md"])
    assert argv == ["cursor", "--reuse-window", "/tmp/project", "/tmp/project/docs/plan.md"]
    assert inspect_macos_open_argv("Cursor", ["/tmp/project/docs/plan.md"]) == [
        "open",
        "-a",
        "Cursor",
        "/tmp/project/docs/plan.md",
    ]


@pytest.mark.asyncio
async def test_complete_open_work_does_not_set_ttl(monkeypatch):
    from plugins.agents import client

    task_doc = {"task_id": "task-1", "status": "completed", "open": True}
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value={"open": True}),
        find_one_and_update=AsyncMock(return_value=task_doc),
        update_one=AsyncMock(),
    )
    monkeypatch.setattr(client, "mongodb", SimpleNamespace(get_collection=lambda _name: collection))
    monkeypatch.setattr(client, "_push_widget", AsyncMock())
    monkeypatch.setattr(client, "_push_task_progress_receipt", AsyncMock())
    monkeypatch.setattr(client, "_publish_completion_trigger", AsyncMock())

    await client._complete_task("task-1", "geoff", "result", "summary", None, None)

    fields = collection.find_one_and_update.await_args.args[1]["$set"]
    assert fields["status"] == "completed"
    assert "expires_at" not in fields


@pytest.mark.asyncio
async def test_close_sets_ttl_on_lineage(monkeypatch):
    from plugins.agents import AgentsPlugin

    plugin = AgentsPlugin()
    collection = _FakeCollection([dict(CONNECT), dict(MICHELL)])
    monkeypatch.setattr("plugins.agents.work.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.mongodb.get_collection", lambda _name: collection)

    result = await plugin.close("2713 review")
    assert "Closed 2713 review" in result
    assert collection.updates[-1][0]["work_id"] == "work-a"
    assert collection.updates[-1][1]["$set"]["open"] is False
    assert "expires_at" in collection.updates[-1][1]["$set"]


@pytest.mark.asyncio
async def test_code_dispatch_refuses_unknown_folder(monkeypatch):
    from plugins.agents import AgentsPlugin, _normalize_agent_cwd

    plugin = AgentsPlugin()
    plugin._semaphore = __import__("asyncio").Semaphore(1)
    assert _normalize_agent_cwd(None) is None
    monkeypatch.setattr("plugins.agents.list_open_work", AsyncMock(return_value=[]))
    monkeypatch.setattr("plugins.agents.list_project_dirs", lambda *_args, **_kwargs: [])

    missing_cwd = json.loads(
        await plugin.dispatch(prompt="look around with no folder", mode="code")
    )
    assert missing_cwd["ok"] is False
    assert missing_cwd["error_code"] == "cwd_required"


@pytest.mark.asyncio
async def test_resume_copies_lineage_and_skips_home_context(monkeypatch):
    from plugins.agents import AgentsPlugin

    plugin = AgentsPlugin()
    plugin._semaphore = __import__("asyncio").Semaphore(1)
    parent = _doc(session_id="claude-session-1", status="completed")
    collection = _FakeCollection([parent, dict(MICHELL)])
    captured: dict = {}

    async def fake_prompt(self, owner_id, cwd, conversation_context=""):
        captured["conversation_context"] = conversation_context
        captured["cwd"] = cwd
        return "sys"

    async def fake_prepare(self, **kwargs):
        captured["extra"] = kwargs.get("extra_doc")
        captured["prompt"] = kwargs.get("prompt")
        return "task-new", collection

    def fake_spawn(self, task_id, coro, source):
        captured["spawned"] = (task_id, source)
        coro.close()

    monkeypatch.setattr("plugins.agents.work.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents._PromptBuilder.build_subprocess_prompt", fake_prompt)
    monkeypatch.setattr("plugins.agents._get_connected_composio_apps", AsyncMock(return_value=[]))
    monkeypatch.setattr("plugins.agents._resolve_tools_for_dispatch", AsyncMock(return_value=[]))
    monkeypatch.setattr("plugins.agents.worker_ready", lambda _kind: True)
    monkeypatch.setattr(AgentsPlugin, "_prepare_task", fake_prepare)
    monkeypatch.setattr(AgentsPlugin, "_spawn_task", fake_spawn)

    payload = json.loads(
        await plugin.resume("2713 review", "Don't overcomplicate it — just the core comments.")
    )

    assert payload["ok"] is True
    assert payload["work_id"] == "work-a"
    assert payload["task_id"] == "task-new"
    assert captured["conversation_context"] == ""
    assert captured["prompt"] == "Don't overcomplicate it — just the core comments."
    assert payload["message"] == "Continuing 2713 review."
    assert captured["extra"]["work_id"] == "work-a"
    assert captured["extra"]["title"] == "2713 review"
    assert captured["extra"]["session_id"] == "claude-session-1"
    assert captured["extra"]["worker_kind"] == "claude_code"
    assert captured["cwd"].endswith("aetheron-connect-v2")


@pytest.mark.asyncio
async def test_resume_treats_constraint_as_target_when_one_open(monkeypatch):
    from plugins.agents import AgentsPlugin

    plugin = AgentsPlugin()
    plugin._semaphore = __import__("asyncio").Semaphore(1)
    parent = _doc(session_id="claude-session-1", status="completed")
    collection = _FakeCollection([parent])
    captured: dict = {}

    async def fake_prompt(self, owner_id, cwd, conversation_context=""):
        return "sys"

    async def fake_prepare(self, **kwargs):
        captured["prompt"] = kwargs.get("prompt")
        captured["extra"] = kwargs.get("extra_doc")
        return "task-new", collection

    def fake_spawn(self, task_id, coro, source):
        coro.close()

    monkeypatch.setattr("plugins.agents.work.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents._PromptBuilder.build_subprocess_prompt", fake_prompt)
    monkeypatch.setattr("plugins.agents._get_connected_composio_apps", AsyncMock(return_value=[]))
    monkeypatch.setattr("plugins.agents._resolve_tools_for_dispatch", AsyncMock(return_value=[]))
    monkeypatch.setattr("plugins.agents.worker_ready", lambda _kind: True)
    monkeypatch.setattr(AgentsPlugin, "_prepare_task", fake_prepare)
    monkeypatch.setattr(AgentsPlugin, "_spawn_task", fake_spawn)

    payload = json.loads(
        await plugin.resume("Don't overcomplicate it — just the core comments.")
    )
    assert payload["ok"] is True
    assert captured["prompt"] == "Don't overcomplicate it — just the core comments."
    assert captured["extra"]["work_id"] == "work-a"


@pytest.mark.asyncio
async def test_dispatch_continues_matching_open_work(monkeypatch):
    from plugins.agents import AgentsPlugin, _dispatch_result

    plugin = AgentsPlugin()
    plugin._semaphore = __import__("asyncio").Semaphore(1)
    captured: dict = {}

    async def fake_resume(self, target, feedback=""):
        captured["target"] = target
        captured["feedback"] = feedback
        return _dispatch_result(
            ok=True, task_id="task-new", work_id="work-a", mode="code",
            message="Continuing 2713 review.",
        )

    monkeypatch.setattr("plugins.agents.list_open_work", AsyncMock(return_value=[dict(CONNECT)]))
    monkeypatch.setattr("plugins.agents.list_project_dirs", lambda *_a, **_k: [])
    monkeypatch.setattr(AgentsPlugin, "resume", fake_resume)

    payload = json.loads(
        await plugin.dispatch(
            prompt="In connect-v2 look at the 2713 review comments. Don't edit yet.",
            mode="code",
        )
    )
    assert payload["ok"] is True
    assert captured["target"] == "work-a"


@pytest.mark.asyncio
async def test_named_work_conductor_loops(monkeypatch):
    from plugins.agents import AgentsPlugin

    plugin = AgentsPlugin()
    plugin._semaphore = __import__("asyncio").Semaphore(1)
    collection = _FakeCollection([dict(CONNECT), dict(MICHELL)])
    monkeypatch.setattr("plugins.agents.work.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.mongodb.get_collection", lambda _name: collection)

    roster = await plugin.get_status()
    assert "2713 review" in roster
    assert "Quoting ports" in roster
    assert "aetheron-connect-v2" in roster
    assert "michell-wool-ai-role-discovery" in roster

    connect_status = await plugin.get_status("what's happening with the 2713 review")
    assert "2713 review" in connect_status
    assert "Still open — resume to continue" in connect_status
    assert "Quoting ports" not in connect_status

    tweak = await plugin.get_status("tweak it")
    assert tweak.code == "ambiguous_work"
    assert "2713 review" in tweak.message
    assert "Quoting ports" in tweak.message

    michell_status = await plugin.get_status("the quoting ports thing")
    assert "Quoting ports" in michell_status

    refused = json.loads(await plugin.dispatch(prompt="look around", title="stray", mode="code"))
    assert refused["ok"] is False
    assert refused["error_code"] == "cwd_required"

    closed = await plugin.close("I'm done with the 2713 review")
    assert "Closed 2713 review" in closed
    remaining = await plugin.get_status()
    assert "2713 review" not in remaining
    assert "Quoting ports" in remaining


@pytest.mark.asyncio
async def test_inspect_opens_claude_and_refuses_running(monkeypatch):
    from plugins.agents import AgentsPlugin

    plugin = AgentsPlugin()
    launched: list[list[str]] = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **_kwargs):
        launched.append(list(argv))
        return _Proc()

    completed = _doc(session_id="claude-session-1", status="completed", mode="code")
    running = _doc(session_id="claude-session-1", status="running", mode="code")
    collection = _FakeCollection([completed])
    monkeypatch.setattr("plugins.agents.work.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.sys.platform", "darwin")
    monkeypatch.setattr("plugins.agents.shutil.which", lambda _name: "/usr/local/bin/claude")
    monkeypatch.setattr("plugins.agents.asyncio.create_subprocess_exec", fake_exec)

    opened = await plugin.inspect("2713 review")
    assert opened == "Opened 2713 review in Claude Code."
    assert launched and launched[0][0] == "osascript"
    assert "claude --resume claude-session-1" in launched[0][2]

    collection.docs = [running]
    refused = await plugin.inspect("2713 review")
    assert refused.code == "work_still_running"

    collection.docs = [_doc(session_id=None, status="completed", mode="code")]
    missing = await plugin.inspect("2713 review")
    assert missing.code == "session_missing"


@pytest.mark.asyncio
async def test_inspect_opens_claude_file_with_macos_open(monkeypatch, tmp_path):
    from plugins.agents import AgentsPlugin

    plugin = AgentsPlugin()
    launched: list[list[str]] = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **_kwargs):
        launched.append(list(argv))
        return _Proc()

    completed = _doc(session_id="claude-session-1", status="completed", mode="code", cwd=str(tmp_path))
    collection = _FakeCollection([completed])
    monkeypatch.setattr("plugins.agents.work.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.sys.platform", "darwin")
    monkeypatch.setattr("plugins.agents.shutil.which", lambda _name: "/usr/local/bin/claude")
    monkeypatch.setattr("plugins.agents.asyncio.create_subprocess_exec", fake_exec)

    opened = await plugin.inspect("2713 review", path="docs/plan.md")
    assert opened == "Opened plan.md."
    assert launched[0] == ["open", str((tmp_path / "docs/plan.md").resolve())]


@pytest.mark.asyncio
async def test_inspect_opens_cursor_for_cursor_lineage(monkeypatch, tmp_path):
    from plugins.agents import AgentsPlugin

    plugin = AgentsPlugin()
    launched: list[list[str]] = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **_kwargs):
        launched.append(list(argv))
        return _Proc()

    cwd = str(tmp_path)
    completed = _doc(
        session_id="agent-1",
        status="completed",
        mode="code",
        worker_kind="cursor_local",
        cwd=cwd,
    )
    collection = _FakeCollection([completed])
    monkeypatch.setattr("plugins.agents.work.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.sys.platform", "darwin")
    monkeypatch.setattr(
        "plugins.agents.shutil.which",
        lambda name: "/usr/local/bin/cursor" if name == "cursor" else None,
    )
    monkeypatch.setattr("plugins.agents.asyncio.create_subprocess_exec", fake_exec)

    opened = await plugin.inspect("2713 review")
    assert opened == "Opened 2713 review in Cursor."
    assert launched[0] == ["cursor", "--reuse-window", str(tmp_path.resolve())]

    launched.clear()
    file_opened = await plugin.inspect("2713 review", path="docs/plan.md")
    assert file_opened == "Opened plan.md in Cursor."
    assert launched[0] == ["cursor", "--reuse-window", str((tmp_path / "docs/plan.md").resolve())]

    denied = await plugin.inspect("2713 review", path="../secret.md")
    assert denied.code == "inspect_path_denied"


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, _n):
        return self

    async def to_list(self, length=None):
        return self.docs[:length] if length else list(self.docs)


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.updates: list[tuple[dict, dict]] = []

    def find(self, filt=None, *_args, **_kwargs):
        return _Cursor([doc for doc in self.docs if self._matches(doc, filt or {})])

    async def find_one(self, filt, _projection=None):
        for doc in self.docs:
            if self._matches(doc, filt):
                return doc
        return None

    async def update_many(self, filt, update):
        self.updates.append((filt, update))
        count = 0
        for doc in self.docs:
            if self._matches(doc, filt):
                doc.update(update.get("$set", {}))
                count += 1
        return SimpleNamespace(modified_count=count)

    @staticmethod
    def _matches(doc, filt):
        return all(doc.get(k) == v for k, v in filt.items() if k != "owner_id")
