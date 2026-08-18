"""
Agents Plugin — JARV1S delegated background work.

Exposes the following tools to the LLM:
  dispatch     — delegate long-running work (returns immediately)
  resume       — resume a completed task with new feedback
  get_status   — status/progress for a single task or all active tasks
  get_result   — retrieve the full result for completed delegated work
  list_tasks   — list tasks filtered by status
  cancel_task  — cancel a running task
"""

import asyncio
import contextvars
import json
import logging
import os
import signal
import subprocess as _sp
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from core.id import generate_id

from core.agent.agent import AgentEventType, JarvisAgent
from core.agent.sdk import _ACTIVE_SDK
from core.config import settings
from core.credentials.store import credential_store
from core.decorators import tool
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.prompts.builder import PromptBuilder as _PromptBuilder
from core.turns.reasoning_effort import resolve_reasoning_effort
from plugins.agents.client import (
    _resolve_tools_for_dispatch,
    _run_agent,
    _push_task_event,
    _push_task_progress_receipt,
    _push_ui_delete,
    _push_ui_envelope,
    _push_widget,
    _publish_approval_needed_trigger,
    _complete_task,
    _fail_task,
)
from plugins.agents.task_review import (
    activity_from_tool_output,
    append_activity,
    append_trace,
    merge_artifacts,
    new_span_id,
    task_trace_item,
    written_artifacts_from_output,
)
from core.plugins.capabilities import (
    CapabilityErrorDetail,
    capability_fqns,
    invocation_trace_payload,
    reset_capability_task_id,
    set_capability_task_id,
)
from services.database.mongodb import mongodb
from core.pending_inputs import (
    create_pending_input,
    pending_input_summary,
    publish_pending_input,
    wait_for_pending_input,
)

logger = logging.getLogger(__name__)

def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


RESTART_INTERRUPTED_RESULT = (
    "Task was interrupted by a backend restart before it completed. "
    "If this was a code-mode task with a saved session_id, use resume() "
    "with follow-up instructions."
)

# Set inside _run_inprocess so any code executed by the in-process agent
# (including tool calls back into dispatch()) knows it's already background.
_IN_BACKGROUND = contextvars.ContextVar("_in_background_dispatch", default=False)

# PIDs of agent subprocesses spawned by this process.
# Populated by _run_agent via register_child_pid; used for targeted cleanup.
_child_pids: set[int] = set()


def _default_agent_cwd() -> str:
    """Default delegated work to the JARV1S project root until a user sandbox exists."""
    return str(settings.BASE_DIR.parent)


def _normalize_agent_cwd(cwd: str | None) -> str:
    if not cwd:
        return _default_agent_cwd()
    return os.path.abspath(os.path.expanduser(cwd))


def _dispatch_result(
    *,
    ok: bool,
    message: str,
    task_id: str | None = None,
    mode: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> str:
    return json.dumps(
        {
            "ok": ok,
            "task_id": task_id,
            "mode": mode,
            "error_code": error_code,
            "error_message": error_message,
            "message": message,
        }
    )


def _dispatch_failure(error_code: str, error_message: str) -> dict[str, str]:
    return {"error_code": error_code, "error_message": error_message}


def register_child_pid(pid: int) -> None:
    _child_pids.add(pid)


def unregister_child_pid(pid: int) -> None:
    _child_pids.discard(pid)


def _force_kill_tracked_children() -> int:
    """SIGKILL any child PIDs we still track. Returns count killed."""
    killed = 0
    for pid in list(_child_pids):
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
            logger.warning("Force-killed tracked agent child pid=%d", pid)
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.debug("Failed to kill pid=%d: %s", pid, exc)
        _child_pids.discard(pid)
    return killed


def _scan_orphans() -> None:
    """Best-effort scan for orphaned opencode/claude processes from previous runs."""
    for name in ("opencode", "claude"):
        try:
            result = _sp.run(
                ["pgrep", "-f", name],
                capture_output=True, text=True, timeout=2,
            )
            pids = [p for p in result.stdout.strip().split() if p]
            if pids:
                logger.warning(
                    "Orphan scan: found %d '%s' process(es) from a previous run: pids=%s",
                    len(pids), name, pids,
                )
        except Exception:
            pass


class AgentsPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="agents",
        version="1.0.0",
        description="Delegate long-running background work across jarvis and code runtimes.",
    )

    def __init__(self):
        self._semaphore: asyncio.Semaphore | None = None
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._source_counts: Counter[str] = Counter()

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        self._semaphore = asyncio.Semaphore(settings.AGENT_MAX_CONCURRENT)
        await self._recover_interrupted_tasks()
        logger.info(
            "AgentsPlugin initialized (max_concurrent=%d, sdk=%s)",
            settings.AGENT_MAX_CONCURRENT,
            _ACTIVE_SDK,
        )
        _scan_orphans()

    async def _recover_interrupted_tasks(self) -> int:
        """Fail task rows that belonged to a prior backend process."""
        now = datetime.now(timezone.utc)
        col = mongodb.get_collection("background_tasks")
        result = await col.update_many(
            {"status": "running"},
            {
                "$set": {
                    "status": "failed",
                    "result": RESTART_INTERRUPTED_RESULT,
                    "progress_summary": "Interrupted by backend restart.",
                    "completed_at": now,
                    "expires_at": now + timedelta(days=30),
                    "interrupted_reason": "backend_restart",
                }
            },
        )
        recovered = int(getattr(result, "modified_count", 0) or 0)
        if recovered:
            logger.warning("Marked %d interrupted background task(s) failed after restart", recovered)
        return recovered

    async def shutdown(self) -> None:
        if not self._running_tasks:
            return
        logger.info("AgentsPlugin shutting down — cancelling %d task(s)…", len(self._running_tasks))
        tasks = list(self._running_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Some agent tasks did not finish within shutdown timeout.")

        # Each task's finally block should have killed its own PID already,
        # but sweep anything still tracked as a safety net.
        killed = _force_kill_tracked_children()
        if killed:
            logger.warning("Force-killed %d lingering agent child(ren) during shutdown", killed)

        self._running_tasks.clear()

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @tool
    async def dispatch(
        self,
        prompt: str,
        cwd: str | None = None,
        mode: str = "code",
        max_turns: int = 40,
        max_budget_usd: float = 1.0,
    ) -> str:
        """
        Delegate long-running work and return immediately.
        Returns a JSON string: {"ok": bool, "task_id": str | null, "mode": str | null,
        "error_code": str | null, "error_message": str | null, "message": str}.
        If ok=false, no task was started.

        Use when work is broad or slow enough to continue while the user moves on:
        inbox triage, calendar/Slack investigations, multi-source research, long file edits,
        git operations, shell pipelines, or tasks likely to need many tool calls or 30+ seconds.
        Do not use for quick lookups, direct controls, simple calculations, or single-call
        operations. Never dispatch just to read a file or check status — do it inline.
        Do NOT dispatch when you are already handling delegated work; use available tools directly.

        After dispatch, move on. Tell the user naturally that you'll handle it and report back
        when there is a result or you need them. Do not poll get_status() immediately.
        If the user later asks what a completed task found or asks you to repeat it, call
        get_result(task_id) or get_result() for the latest result. Do not dispatch the same
        work again unless the user explicitly asks you to rerun it.

        One dispatch call starts at most one task. For batches, call dispatch once per task
        and inspect each JSON result before starting the next task. Use resume(task_id, feedback)
        instead of dispatch when continuing a previous code-mode task.

        The delegated agent has no memory of this conversation. Write a focused prompt:
        include relevant context, absolute paths, expected output, where to save artifacts,
        whether files already exist, constraints, and what "done" means. Avoid vague goals.

        TWO RUNTIMES, BY DESIGN — see docs/BACKGROUND_AGENTS.md for the rationale.
        mode="code" — Claude Agent SDK subprocess. Powerful coding model with Bash + file
            editing only; no jarvis.* bridge. SDK-isolated; does not use jarvis consent.
            Best fit for: repository work, docs/code review, file creation/editing,
            Git operations, shell tasks, or writing artifacts in the repo — even
            when the subject is JARV1S itself.
        mode="jarvis" — in-process capability-call loop. Shares live integrations with the voice
            loop (Slack, Gmail, GitHub, Calendar, Memory, Smart Home, etc.), zero IPC overhead,
            destructive actions can pause on PendingInputWidget approval.
            Best fit for: slow assistant-world work involving live integrations,
            API tools, JARV1S plugins, user context, widgets, automations, or
            cross-service evidence.
        Choose "code" for repo/docs/code analysis, file/git/shell work, or artifacts
        that have no live JARV1S plugin dependency. Choose "jarvis" when the task
        needs live JARV1S runtime integrations. Do quick single integration lookups
        inline; dispatch broad scans or synthesis in mode="jarvis".

        max_turns: cap on agent iterations (default 40). Reduce for simple focused tasks.
        max_budget_usd: spending cap in USD (default $1.00).
        """
        if _IN_BACKGROUND.get(False):
            return _dispatch_result(
                ok=False,
                error_code="already_in_background",
                error_message=(
                    "You are already handling delegated work. Execute the task directly using "
                    "available tools instead of re-dispatching."
                ),
                message="Task not started: already handling delegated work.",
            )

        # Normalize paths: ~ cannot be reliably resolved inside the agent subprocess.
        home = os.path.expanduser("~")
        prompt = prompt.replace("~/", f"{home}/")
        cwd = _normalize_agent_cwd(cwd)

        if mode == "jarvis":
            if await self._get_background_agent() is None:
                logger.warning("dispatch mode='jarvis' requested but jarvis runtime is unavailable")
                return _dispatch_result(
                    ok=False,
                    error_code="jarvis_runtime_unavailable",
                    error_message="Jarvis-mode dispatch is unavailable; integration tasks cannot start.",
                    message="Task not started: jarvis-mode dispatch is unavailable.",
                )
            else:
                return await self._dispatch_inprocess(
                    prompt=prompt, cwd=cwd, max_budget_usd=max_budget_usd
                )
        if not credential_store.get_stored_secret("ANTHROPIC_API_KEY"):
            return _dispatch_result(
                ok=False,
                error_code="background_credentials_unavailable",
                error_message="Add an Anthropic API key in Settings to enable background agents.",
                message="Task not started: background-agent credentials are unavailable.",
            )
        return await self._dispatch(prompt=prompt, cwd=cwd, max_turns=max_turns, max_budget_usd=max_budget_usd)

    @tool
    async def resume(self, task_id: str, feedback: str) -> str:
        """
        Continue a previous code-mode task with new instructions.
        Uses the saved SDK session_id when available, preserving prior file reads,
        edits, and decisions. If the session file is missing, the SDK may start fresh.

        Use when the user wants to modify, extend, or fix what a previous code-mode
        agent produced. Do not use for unrelated work or for jarvis-mode tasks that
        depended on live JARV1S plugin state; dispatch a fresh mode="jarvis" task instead.

        task_id: the ID returned by the original dispatch().
        feedback: the new instructions for the agent.
        """
        if self._semaphore is None:
            return _fail("AgentsPlugin not initialized.")

        col = mongodb.get_collection("background_tasks")
        doc = await col.find_one({"task_id": task_id})
        if not doc:
            return _fail(f"Task {task_id} not found.")
        if doc["status"] == "running":
            return _fail(f"Task {task_id} is still running. Use cancel_task first if you want to redirect it.")
        if not credential_store.get_stored_secret("ANTHROPIC_API_KEY"):
            return _dispatch_result(
                ok=False,
                error_code="background_credentials_unavailable",
                error_message="Add an Anthropic API key in Settings to resume background agents.",
                message="Resume not started: background-agent credentials are unavailable.",
            )

        cwd = doc.get("cwd", str(settings.BASE_DIR.parent))
        max_turns = doc.get("max_turns", 40)
        max_budget_usd = doc.get("max_budget_usd", 1.0)
        resume_session_id = doc.get("session_id")

        _pb = _PromptBuilder()
        connected_apps, conv_context = await asyncio.gather(
            _get_connected_composio_apps(),
            _PromptBuilder.build_conversation_context(settings.DEFAULT_USER_ID),
        )
        mcp_servers, system_prompt = await asyncio.gather(
            _resolve_tools_for_dispatch(connected_apps),
            _pb.build_subprocess_prompt(settings.DEFAULT_USER_ID, cwd, conv_context),
        )

        result = await self._prepare_task(
            prompt=feedback, cwd=cwd, mode="code", max_turns=max_turns,
            max_budget_usd=max_budget_usd, source="resume",
            trigger_ref=doc.get("trigger_ref"), depth=0,
            extra_doc={"mcp_servers": mcp_servers, "progress_summary": "Resuming…"},
        )
        if isinstance(result, dict):
            return _dispatch_result(
                ok=False,
                error_code=result["error_code"],
                error_message=result["error_message"],
                message=f"Resume not started: {result['error_message']}",
            )
        new_task_id, _ = result

        owner_id = settings.DEFAULT_USER_ID

        async def _run():
            async with self._semaphore:
                await _run_agent(
                    task_id=new_task_id,
                    owner_id=owner_id,
                    prompt=feedback,
                    cwd=cwd,
                    max_turns=max_turns,
                    mcp_servers=mcp_servers,
                    system_prompt=system_prompt,
                    resume_session_id=resume_session_id,
                    max_budget_usd=max_budget_usd,
                )

        self._spawn_task(new_task_id, _run(), "resume")
        return f"Resuming task. new_task_id={new_task_id}"

    @tool
    async def get_status(self, task_id: str | None = None) -> str:
        """
        Get status and progress for delegated work, or list everything currently running.
        task_id: omit to see all running tasks.
        For completed or failed tasks, the status is terminal; do not poll again.
        Call get_result(task_id) to retrieve the full result.
        """
        col = mongodb.get_collection("background_tasks")
        if task_id:
            doc = await col.find_one({"task_id": task_id}, {"events": 0})
            if not doc:
                return _fail(f"Task {task_id} not found.")
            status = doc["status"]
            if status in {"completed", "failed"}:
                return (
                    f"Task {task_id}: status={status} (terminal). "
                    f"Do not poll status again; call "
                    f"jarvis.agents.get_result(task_id=\"{task_id}\") for the full result."
                )
            return (
                f"Task {task_id}: status={status}, "
                f"progress={doc.get('progress_summary', '')}"
            )

        cursor = col.find(
            {"owner_id": settings.DEFAULT_USER_ID, "status": "running"},
            {"task_id": 1, "progress_summary": 1, "source": 1},
        ).limit(10)
        docs = await cursor.to_list(length=10)
        if not docs:
            return "Nothing is currently in progress."
        lines = [
            f"- {d['task_id']}: {d.get('progress_summary', '')} ({d.get('source', '')})"
            for d in docs
        ]
        return "Active tasks:\n" + "\n".join(lines)

    @tool
    async def get_result(self, task_id: str | None = None) -> str:
        """
        Retrieve the full stored result for completed or failed delegated work.
        Use this when the user asks what a background task found, asks you to repeat
        a result, or refers to a just-finished task whose spoken delivery was interrupted.
        If task_id is omitted, returns the most recent completed or failed task.
        Do not call dispatch() to recover a result unless the user explicitly asks to rerun work.
        If artifacts is non-empty and the user refers to "that file" or "the output",
        use artifacts[0].path as the exact target path for follow-up file tools.
        Returns a JSON string: {"ok": bool, "task_id": str | null, "status": str | null,
        "result": str | null, "artifacts": list, "error_code": str | null,
        "error_message": str | null, "message": str}.
        """
        col = mongodb.get_collection("background_tasks")
        projection = {
            "_id": 0,
            "task_id": 1,
            "status": 1,
            "result": 1,
            "progress_summary": 1,
            "artifacts": 1,
        }
        if task_id:
            doc = await col.find_one(
                {"task_id": task_id, "owner_id": settings.DEFAULT_USER_ID},
                projection,
            )
        else:
            doc = await col.find_one(
                {
                    "owner_id": settings.DEFAULT_USER_ID,
                    "status": {"$in": ["completed", "failed"]},
                },
                projection,
                sort=[("completed_at", -1), ("created_at", -1)],
            )

        if not doc:
            return json.dumps(
                {
                    "ok": False,
                    "task_id": task_id,
                    "status": None,
                    "result": None,
                    "artifacts": [],
                    "error_code": "task_result_not_found",
                    "error_message": "No matching completed or failed task result was found.",
                    "message": "Task result not found.",
                }
            )

        status = doc.get("status")
        if status == "running":
            return json.dumps(
                {
                    "ok": False,
                    "task_id": doc.get("task_id"),
                    "status": status,
                    "result": None,
                    "artifacts": [],
                    "error_code": "task_still_running",
                    "error_message": "The task is still running; use get_status() for progress.",
                    "message": "Task is still running.",
                }
            )

        result = doc.get("result") or doc.get("progress_summary") or ""
        return json.dumps(
            {
                "ok": True,
                "task_id": doc.get("task_id"),
                "status": status,
                "result": result,
                "artifacts": doc.get("artifacts") or [],
                "error_code": None,
                "error_message": None,
                "message": "Task result found.",
            }
        )

    @tool
    async def list_tasks(self, status: str | None = None) -> list[dict]:
        """
        List delegated work, optionally filtered by status: "running", "completed", or "failed".
        """
        col = mongodb.get_collection("background_tasks")
        filt: dict[str, Any] = {"owner_id": settings.DEFAULT_USER_ID}
        if status:
            filt["status"] = status

        cursor = col.find(filt, {"events": 0, "_id": 0}).sort("created_at", -1).limit(20)
        return await cursor.to_list(length=20)

    @tool
    async def cancel_task(self, task_id: str) -> str:
        """Cancel a running delegated work item."""
        task = self._running_tasks.get(task_id)
        if not task:
            col = mongodb.get_collection("background_tasks")
            doc = await col.find_one({"task_id": task_id}, {"status": 1})
            if not doc:
                return _fail(f"Task {task_id} not found.")
            return _fail(f"Task {task_id} is not running (status={doc['status']}).")

        task.cancel()
        return f"Cancellation requested for task {task_id}."

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _prepare_task(
        self,
        *,
        prompt: str,
        cwd: str,
        mode: str,
        max_turns: int,
        max_budget_usd: float,
        source: str,
        trigger_ref: str | None,
        depth: int,
        extra_doc: dict | None = None,
    ) -> tuple[str, Any] | dict[str, str]:
        """Guards + DB insert shared by both dispatch paths.

        Returns (task_id, collection) on success, or an error dict on failure.
        """
        if self._semaphore is None:
            return _dispatch_failure("agents_not_initialized", "AgentsPlugin is not initialized.")
        if depth >= settings.AGENT_MAX_DEPTH:
            return _dispatch_failure(
                "depth_limit_reached",
                (
                    f"Depth limit ({settings.AGENT_MAX_DEPTH}) reached. "
                    "Background agents cannot spawn further agents."
                ),
            )
        if self._source_counts[source] >= settings.AGENT_MAX_PER_SOURCE:
            return _dispatch_failure(
                "source_limit_reached",
                (
                    f"Source '{source}' already has {self._source_counts[source]} active "
                    f"task(s) (limit: {settings.AGENT_MAX_PER_SOURCE})."
                ),
            )

        task_id = generate_id()
        now = datetime.now(timezone.utc)
        col = mongodb.get_collection("background_tasks")
        doc: dict[str, Any] = {
            "task_id": task_id,
            "owner_id": settings.DEFAULT_USER_ID,
            "status": "running",
            "mode": mode,
            "prompt": prompt,
            "cwd": cwd,
            "source": source,
            "trigger_ref": trigger_ref,
            "progress_summary": "Starting…",
            "live_status": "Starting…",
            "attention": "none",
            "pending_input": None,
            "events": [],
            "artifacts": [],
            "activity": [],
            "max_turns": max_turns,
            "max_budget_usd": max_budget_usd,
            "depth": depth,
            "cost_usd": None,
            "result": None,
            "session_id": None,
            "created_at": now,
            "completed_at": None,
        }
        if extra_doc:
            doc.update(extra_doc)
        await col.insert_one(doc)
        self._source_counts[source] += 1
        return task_id, col

    async def _get_background_agent(self) -> JarvisAgent | None:
        """Resolve the background agent from the IntegrationManager at call-time."""
        try:
            from core.integrations.manager import integrations
            return await integrations.get("background_agent")
        except (KeyError, Exception):
            return None

    async def _dispatch(
        self,
        prompt: str,
        cwd: str,
        max_turns: int,
        max_budget_usd: float,
        source: str = "voice",
        trigger_ref: str | None = None,
        depth: int = 0,
    ) -> str:
        """Shared implementation for dispatch() and automation-triggered calls."""
        _pb = _PromptBuilder()
        connected_apps, conv_context = await asyncio.gather(
            _get_connected_composio_apps(),
            _PromptBuilder.build_conversation_context(settings.DEFAULT_USER_ID),
        )
        mcp_servers, system_prompt = await asyncio.gather(
            _resolve_tools_for_dispatch(connected_apps),
            _pb.build_subprocess_prompt(settings.DEFAULT_USER_ID, cwd, conv_context),
        )

        result = await self._prepare_task(
            prompt=prompt, cwd=cwd, mode="code", max_turns=max_turns,
            max_budget_usd=max_budget_usd, source=source, trigger_ref=trigger_ref,
            depth=depth, extra_doc={"mcp_servers": mcp_servers},
        )
        if isinstance(result, dict):
            return _dispatch_result(
                ok=False,
                error_code=result["error_code"],
                error_message=result["error_message"],
                message=f"Task not started: {result['error_message']}",
            )
        task_id, _ = result

        await _push_task_progress_receipt(settings.DEFAULT_USER_ID, task_id, force=True)

        async def _run():
            async with self._semaphore:
                await _run_agent(
                    task_id=task_id,
                    owner_id=settings.DEFAULT_USER_ID,
                    prompt=prompt,
                    cwd=cwd,
                    max_turns=max_turns,
                    mcp_servers=mcp_servers,
                    system_prompt=system_prompt,
                    max_budget_usd=max_budget_usd,
                )

        self._spawn_task(task_id, _run(), source)
        logger.info("Dispatched agent task %s (source=%s, depth=%d)", task_id, source, depth)
        return _dispatch_result(
            ok=True,
            task_id=task_id,
            mode="code",
            message="Task started.",
        )

    def _spawn_task(
        self,
        task_id: str,
        coro: Any,
        source: str,
    ) -> None:
        """Register an asyncio.Task for lifecycle tracking (cancellation, done callback).

        Maintains a strong reference in _running_tasks to prevent GC of the task
        before it completes.
        """
        def _on_done(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            self._source_counts.subtract([source])

        task = asyncio.create_task(coro, name=f"agent-{task_id}")
        self._running_tasks[task_id] = task
        task.add_done_callback(_on_done)

    async def _dispatch_inprocess(
        self,
        prompt: str,
        cwd: str,
        max_budget_usd: float = 1.0,
        source: str = "voice",
        trigger_ref: str | None = None,
        depth: int = 0,
    ) -> str:
        """In-process background dispatch: runs background_agent.process_stream() as an asyncio.Task."""
        background_agent = await self._get_background_agent()
        if background_agent is None:
            return _dispatch_result(
                ok=False,
                error_code="jarvis_runtime_unavailable",
                error_message="Jarvis-mode dispatch is unavailable; integration tasks cannot start.",
                message="Task not started: jarvis-mode dispatch is unavailable.",
            )

        result = await self._prepare_task(
            prompt=prompt, cwd=cwd, mode="jarvis",
            max_turns=settings.AGENT_INPROCESS_MAX_TURNS,
            max_budget_usd=max_budget_usd, source=source, trigger_ref=trigger_ref,
            depth=depth,
        )
        if isinstance(result, dict):
            return _dispatch_result(
                ok=False,
                error_code=result["error_code"],
                error_message=result["error_message"],
                message=f"Task not started: {result['error_message']}",
            )
        task_id, col = result

        owner_id = settings.DEFAULT_USER_ID
        await _push_task_progress_receipt(owner_id, task_id, force=True)

        async def _run_inprocess():
            _IN_BACKGROUND.set(True)
            from core.plugins.consent import _consent_resolver
            _resolver_token = None
            task_token = set_capability_task_id(task_id)
            async with self._semaphore:
                try:
                    from core.prompts.builder import PromptMode
                    from core.tool_router import tool_router

                    bg_context = await _PromptBuilder.build_background_context(owner_id, cwd)

                    # Route the dispatch prompt so this background run's tools=
                    # set matches the task instead of carrying an empty match set.
                    bg_context["routed_tools"] = await tool_router.route(
                        prompt,
                        task_id,
                    )

                    last_text = ""
                    artifacts: list[dict[str, Any]] = []
                    activity: list[dict[str, Any]] = []
                    trace: list[dict[str, Any]] = []
                    reasoning_buffer = ""
                    current_span_id: str | None = None
                    current_tool_label = "tool"

                    def _flush_reasoning_trace() -> bool:
                        nonlocal trace, reasoning_buffer
                        text = reasoning_buffer.strip()
                        if not text:
                            return False
                        trace = append_trace(
                            trace,
                            task_trace_item(
                                kind="reasoning",
                                ts=int(datetime.now(timezone.utc).timestamp() * 1000),
                                text_preview=text,
                                status="completed",
                            ),
                        )
                        reasoning_buffer = ""
                        return True

                    async def _deferred_background_approval(desc: str, detail: str) -> bool:
                        nonlocal trace
                        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                        pending = await create_pending_input(
                            owner_id=owner_id,
                            prompt=desc,
                            detail=detail,
                            source={"type": "background_task", "id": task_id, "task_id": task_id},
                            create_waiter=True,
                            publish="event_bus",
                        )
                        summary_doc = pending_input_summary(pending)
                        trace = append_trace(
                            trace,
                            task_trace_item(
                                kind="approval_requested",
                                ts=ts,
                                text_preview=desc,
                                status="running",
                            ),
                        )
                        task_doc = await col.find_one_and_update(
                            {"task_id": task_id},
                            {
                                "$set": {
                                    "attention": "approval",
                                    "pending_input": summary_doc,
                                    "live_status": "Waiting for approval…",
                                    "progress_summary": "Waiting for approval…",
                                    "trace": trace,
                                }
                            },
                            return_document=True,
                        )
                        payload = {
                            "event_type": "approval_requested",
                            "text": desc[:500],
                            "ts": ts,
                        }
                        await _push_task_event(owner_id, task_id, payload)
                        if task_doc:
                            task_doc.pop("_id", None)
                            await _push_widget(owner_id, task_id, task_doc)
                            await _push_task_progress_receipt(owner_id, task_id, force=True)
                        try:
                            await _publish_approval_needed_trigger(
                                owner_id=owner_id,
                                task_id=task_id,
                                input_id=pending["input_id"],
                                prompt=desc,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Task %s approval trigger enqueue failed: %s",
                                task_id,
                                exc,
                            )

                        decision = await wait_for_pending_input(pending["input_id"])
                        resolved_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                        approved = decision == "approved"
                        trace = append_trace(
                            trace,
                            task_trace_item(
                                kind="approval_resolved",
                                ts=resolved_ts,
                                text_preview=f"{desc} — {decision}",
                                status="completed" if approved else "failed",
                            ),
                        )
                        task_doc = await col.find_one_and_update(
                            {"task_id": task_id},
                            {
                                "$set": {
                                    "attention": "none",
                                    "pending_input": None,
                                    "live_status": "Approval received." if approved else "Approval denied.",
                                    "progress_summary": "Approval received." if approved else "Approval denied.",
                                    "trace": trace,
                                }
                            },
                            return_document=True,
                        )
                        await _push_task_event(
                            owner_id,
                            task_id,
                            {
                                "event_type": "approval_resolved",
                                "text": decision,
                                "ts": resolved_ts,
                            },
                        )
                        if task_doc:
                            task_doc.pop("_id", None)
                            await _push_widget(owner_id, task_id, task_doc)
                            await _push_task_progress_receipt(owner_id, task_id, force=True)
                        if not approved:
                            await publish_pending_input(
                                owner_id,
                                {**pending, "status": decision},
                                result="Approval denied." if decision == "denied" else "Approval expired.",
                            )
                        return approved

                    _resolver_token = _consent_resolver.set(_deferred_background_approval)

                    async for event in background_agent.process_stream(
                        prompt,
                        [],
                        owner_id,
                        context=bg_context,
                        max_iterations=settings.AGENT_INPROCESS_MAX_TURNS,
                        prompt_mode=PromptMode.BACKGROUND,
                        reasoning_effort=resolve_reasoning_effort(
                            audio_bound=False,
                            text_input=False,
                            headless=True,
                            llm=background_agent.llm,
                        ),
                    ):
                        if event.type == AgentEventType.REASONING:
                            reasoning_buffer += event.content
                            continue

                        if event.type == AgentEventType.UI_UPDATE:
                            await _push_ui_envelope(owner_id, json.loads(event.content))
                            continue

                        if event.type == AgentEventType.UI_DELETE:
                            await _push_ui_delete(owner_id, event.content)
                            continue

                        if event.type == AgentEventType.ERROR:
                            ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                            trace = append_trace(
                                trace,
                                task_trace_item(
                                    kind="error",
                                    ts=ts,
                                    text_preview=event.content,
                                    status="failed",
                                ),
                            )
                            await col.update_one({"task_id": task_id}, {"$set": {"trace": trace}})
                            await _fail_task(task_id, owner_id, event.content)
                            return

                        if event.type == AgentEventType.TEXT:
                            last_text += event.content
                            ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                            payload = {
                                "event_type": "text",
                                "text": event.content[:500],
                                "ts": ts,
                            }
                            trace = append_trace(
                                trace,
                                task_trace_item(
                                    kind="text",
                                    ts=ts,
                                    text_preview=event.content,
                                    status="completed",
                                ),
                            )
                            await col.update_one(
                                {"task_id": task_id},
                                {
                                    "$push": {"events": {"$each": [payload], "$slice": -50}},
                                    "$set": {
                                        "trace": trace,
                                        "live_status": event.content[:100],
                                        "progress_summary": event.content[:100],
                                    },
                                },
                            )
                            await _push_task_event(owner_id, task_id, payload)

                        elif event.type == AgentEventType.TOOL_CALL:
                            _flush_reasoning_trace()
                            last_text = ""
                            ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                            current_span_id = new_span_id()
                            current_tool_label = event.capability or "tool"
                            trace = append_trace(
                                trace,
                                task_trace_item(
                                    kind="tool_call",
                                    ts=ts,
                                    span_id=current_span_id,
                                    tool=current_tool_label,
                                    code=str(event.content),
                                    args_preview=event.arguments,
                                    status="running",
                                ),
                            )
                            payload = {
                                "event_type": "tool_start",
                                "tool": current_tool_label,
                                "ts": ts,
                            }
                            await col.update_one(
                                {"task_id": task_id},
                                {
                                    "$push": {"events": {"$each": [payload], "$slice": -50}},
                                    "$set": {
                                        "trace": trace,
                                        "live_status": f"Running {current_tool_label}…",
                                        "progress_summary": f"Running {current_tool_label}…",
                                    },
                                },
                            )
                            await _push_task_event(owner_id, task_id, payload)

                        elif event.type == AgentEventType.TOOL_OUTPUT:
                            output = str(event.content)
                            ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                            invocations = (
                                [event.outcome.invocation]
                                if event.outcome is not None and event.outcome.invocation is not None
                                else []
                            )
                            capability_fqns_list = capability_fqns(invocations)
                            if capability_fqns_list:
                                current_tool_label = ", ".join(capability_fqns_list)
                            elif event.capability:
                                current_tool_label = event.capability
                            activity = append_activity(
                                activity,
                                activity_from_tool_output(output, source="jarvis"),
                            )
                            trace = append_trace(
                                trace,
                                task_trace_item(
                                    kind="tool_result",
                                    ts=ts,
                                    span_id=new_span_id(),
                                    parent_id=current_span_id,
                                    tool=current_tool_label,
                                    result_preview=output,
                                    status=_task_status_from_invocations(invocations),
                                    args_preview={
                                        "invocations": invocation_trace_payload(invocations),
                                    } if invocations else None,
                                ),
                            )
                            new_artifacts = written_artifacts_from_output(output, cwd=cwd, source="jarvis")
                            if new_artifacts:
                                artifacts = merge_artifacts(artifacts, new_artifacts)
                            payload = {
                                "event_type": "tool_output",
                                "text": output[:500],
                                "tool": current_tool_label,
                                "ts": ts,
                            }
                            update: dict[str, Any] = {
                                "$push": {"events": {"$each": [payload], "$slice": -50}},
                            }
                            set_fields: dict[str, Any] = {"trace": trace}
                            if activity:
                                set_fields["activity"] = activity
                            if artifacts:
                                set_fields["artifacts"] = artifacts
                            update["$set"] = set_fields
                            await col.update_one({"task_id": task_id}, update)
                            await _push_task_event(owner_id, task_id, payload)
                            current_span_id = None
                            current_tool_label = "tool"

                    if _flush_reasoning_trace():
                        await col.update_one(
                            {"task_id": task_id},
                            {"$set": {"trace": trace}},
                        )

                    # last_text = text emitted after the final tool call = the agent's summary.
                    # If empty, the agent ended on a tool call with no trailing text — don't
                    # scrape the execution trace as a fallback, that just surfaces bridge phrases.
                    summary = last_text.strip()
                    if not summary:
                        logger.debug(
                            "In-process task %s: no final text after last tool call — "
                            "tighten background.yaml prompt if this recurs",
                            task_id,
                        )
                    await col.update_one(
                        {"task_id": task_id},
                        {"$set": {"progress_summary": summary[:100] if summary else "Completed."}},
                    )
                    await _complete_task(
                        task_id, owner_id,
                        result=summary or "(task completed — no summary produced)",
                        summary=summary,
                        session_id=None,
                        cost_usd=None,
                    )

                except asyncio.CancelledError:
                    await _fail_task(task_id, owner_id, "Task was cancelled.")
                    raise
                except Exception as e:
                    logger.error("In-process agent task %s failed: %s", task_id, e, exc_info=True)
                    await _fail_task(task_id, owner_id, str(e))
                finally:
                    if _resolver_token is not None:
                        _consent_resolver.reset(_resolver_token)
                    reset_capability_task_id(task_token)

        self._spawn_task(task_id, _run_inprocess(), source)
        logger.info("Dispatched in-process agent task %s (source=%s, depth=%d)", task_id, source, depth)
        return _dispatch_result(
            ok=True,
            task_id=task_id,
            mode="jarvis",
            message="Task started.",
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _task_status_from_invocations(invocations: list[Any]) -> str:
    """Derive task status from the harness invocation ledger."""
    statuses = {
        getattr(record, "status", None).value
        if hasattr(getattr(record, "status", None), "value")
        else (record.get("status") if isinstance(record, dict) else None)
        for record in invocations
    }
    statuses.discard(None)
    if "interrupted" in statuses:
        return "failed"
    if "failed" in statuses or "not_executed" in statuses:
        return "failed"
    if "blocked" in statuses and statuses <= {"blocked", "succeeded"}:
        return "completed"
    if invocations and statuses <= {"succeeded"}:
        return "completed"
    return "completed"


async def _get_connected_composio_apps() -> list[str]:
    """Return list of Composio app names that are currently connected."""
    try:
        from core.integrations.composio_gateway import get_composio_gateway
        gw = get_composio_gateway()
        if not gw:
            return []
        return await gw.list_connected_apps()
    except Exception as e:
        logger.debug("Could not list connected Composio apps: %s", e)
        return []

