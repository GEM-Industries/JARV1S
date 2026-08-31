"""
Agents Plugin — JARV1S delegated background work.

Exposes the following tools to the LLM:
  dispatch     — delegate long-running work (returns immediately)
  resume       — continue named work with new feedback
        Inspect      — open the worker session so the user can read it
  get_status   — status for a title/id, or the recency roster
  get_result   — retrieve the full result for completed delegated work
  list_tasks   — list open work, or filter runs by status
  cancel_task  — cancel a running run (does not close the work)
  close        — forget a work lineage (optional; not required when a run finishes)
"""

import asyncio
import contextvars
import json
import logging
import os
import shutil
import signal
import subprocess as _sp
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from core.id import generate_id

from core.agent.agent import AgentEventType, JarvisAgent
from core.agent.sdk import _ACTIVE_SDK
from core.config import settings
from core.decorators import tool
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.prompts.background import build_background_context
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
    _cancel_task,
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
from plugins.agents.work import (
    WorkResolve,
    compact_task,
    display_cwd,
    format_candidates,
    format_cwd_help,
    format_status,
    infer_title,
    inspect_ide_argv,
    inspect_launch_argv,
    inspect_macos_open_argv,
    inspect_resume_command,
    path_under_cwd,
    list_open_work,
    list_project_dirs,
    load_open_roster,
    load_owner_docs,
    match_open_work,
    resolve_folder,
    resolve_from_docs,
    resolve_steer,
    resolve_target,
    ttl_at,
)
from plugins.agents.workers import (
    default_worker_kind,
    lineage_worker_kind,
    missing_credential_message,
    worker_for_kind,
    worker_ready,
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


def _normalize_agent_cwd(cwd: str | None) -> str | None:
    if not cwd or not str(cwd).strip():
        return None
    return os.path.abspath(os.path.expanduser(str(cwd).strip()))


def _dispatch_result(
    *,
    ok: bool,
    message: str,
    task_id: str | None = None,
    work_id: str | None = None,
    mode: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> str:
    return json.dumps(
        {
            "ok": ok,
            "task_id": task_id,
            "work_id": work_id,
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
        shared = {
            "status": "failed",
            "result": RESTART_INTERRUPTED_RESULT,
            "progress_summary": "Interrupted by backend restart.",
            "completed_at": now,
            "interrupted_reason": "backend_restart",
        }
        open_result = await col.update_many(
            {"status": "running", "open": True},
            {"$set": shared},
        )
        legacy_result = await col.update_many(
            {"status": "running", "open": {"$ne": True}},
            {"$set": {**shared, "expires_at": now + timedelta(days=30)}},
        )
        recovered = int(getattr(open_result, "modified_count", 0) or 0) + int(
            getattr(legacy_result, "modified_count", 0) or 0
        )
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
        title: str | None = None,
        ref: str | None = None,
        mode: str = "code",
        max_turns: int = 40,
        max_budget_usd: float = 1.0,
    ) -> str:
        """
        Delegate long-running work and return immediately.
        Returns JSON: ok, task_id, work_id, mode, error_code, error_message, message.
        If ok=false, no task was started.

        Use when work is broad or slow enough to continue while the user moves on:
        repo investigation, PR review, file/git/shell work, inbox triage, or 30+ second jobs.
        Pass cwd as the project folder or its nickname. Title is optional.
        If that folder or title is already open, this continues it (same as resume).
        Do not files.find, files.read, or system.exec the repo in this Home turn.
        Never search from the JARV1S tree. Do not use for quick lookups or direct controls.
        Never dispatch just to read status. Do NOT dispatch when already handling delegated work.

        mode="code" — local coding worker (Cursor if connected, otherwise Claude) for file/git/shell work.
        mode="jarvis" — in-process plugins (Slack, Gmail, calendar). Fire-and-forget; no resume.
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

        home = os.path.expanduser("~")
        prompt = prompt.replace("~/", f"{home}/")
        named_title = (title or "").strip()
        named_ref = (ref or "").strip() or None

        if mode == "jarvis":
            cwd = _normalize_agent_cwd(cwd)
            if await self._get_background_agent() is None:
                logger.warning("dispatch mode='jarvis' requested but jarvis runtime is unavailable")
                return _dispatch_result(
                    ok=False,
                    error_code="jarvis_runtime_unavailable",
                    error_message="Jarvis-mode dispatch is unavailable; integration tasks cannot start.",
                    message="Task not started: jarvis-mode dispatch is unavailable.",
                )
            return await self._dispatch_inprocess(
                prompt=prompt,
                cwd=cwd or home,
                max_budget_usd=max_budget_usd,
            )

        try:
            open_docs = await list_open_work(settings.DEFAULT_USER_ID)
        except Exception:
            logger.debug("Open work unavailable for dispatch match", exc_info=True)
            open_docs = []
        known = list_project_dirs([str(doc.get("cwd") or "") for doc in open_docs])
        existing = match_open_work(
            open_docs, title=named_title or None, cwd=cwd, prompt=prompt
        )
        if existing.status == "ambiguous":
            return _fail(format_candidates(existing.candidates), "ambiguous_work")
        if existing.status == "single" and existing.doc:
            return await self.resume(
                str(existing.doc.get("work_id") or existing.doc.get("task_id")),
                prompt,
            )

        folder = resolve_folder(cwd, known)
        if folder.status != "single":
            folder = resolve_folder(prompt, known)
        if folder.status == "ambiguous":
            listed = ", ".join(display_cwd(path) for path in folder.candidates)
            return _dispatch_result(
                ok=False,
                error_code="cwd_required",
                error_message=f"Which folder? {listed}",
                message="Task not started: cwd is ambiguous.",
            )
        if folder.status != "single" or not folder.path:
            return _dispatch_result(
                ok=False,
                error_code="cwd_required",
                error_message=format_cwd_help(known),
                message="Task not started: cwd is required for code-mode work.",
            )
        cwd = folder.path
        named_title = infer_title(named_title, cwd, prompt)
        worker_kind = default_worker_kind()
        if worker_kind is None:
            return _dispatch_result(
                ok=False,
                error_code="background_credentials_unavailable",
                error_message=missing_credential_message(),
                message="Task not started: background-agent credentials are unavailable.",
            )
        return await self._dispatch(
            prompt=prompt,
            cwd=cwd,
            title=named_title,
            ref=named_ref,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            worker_kind=worker_kind,
        )

    async def _lookup_work(self, target: str | None) -> dict[str, Any] | CapabilityErrorDetail:
        return self._resolved_doc(await resolve_target(settings.DEFAULT_USER_ID, target))

    def _resolved_doc(self, resolved: WorkResolve) -> dict[str, Any] | CapabilityErrorDetail:
        if resolved.status == "ambiguous":
            return _fail(format_candidates(resolved.candidates), "ambiguous_work")
        if resolved.status != "single" or not resolved.doc:
            return _fail(
                "No matching open work. Call get_status() with no target to list titles.",
                "work_not_found",
            )
        return resolved.doc

    @tool
    async def resume(self, target: str, feedback: str = "") -> str:
        """
        Continue named code-mode work with new instructions.
        target is a title, ref, work_id, or task_id. Prefer the title the user said.
        If the user only gave a constraint and there is one open item, pass that constraint as target.
        Uses the saved worker session. Do not dispatch a new task for the same work.
        Feedback is only the new constraint, not a full re-brief.
        Refuses while a run is still running. Does not close the work.
        """
        if self._semaphore is None:
            return _fail("AgentsPlugin not initialized.")

        instruction = (feedback or "").strip()
        docs = await load_owner_docs(settings.DEFAULT_USER_ID)
        resolved = resolve_from_docs(docs, target)
        if resolved.status == "none" and not instruction:
            instruction = str(target or "").strip()
            resolved = resolve_steer(docs)
        doc = self._resolved_doc(resolved)
        if isinstance(doc, CapabilityErrorDetail):
            return doc
        if not instruction:
            return _fail("Say what to change next on this work.", "feedback_required")
        if doc.get("mode") == "jarvis":
            return _fail(
                "Jarvis-mode work cannot be resumed. Dispatch a fresh mode=jarvis task if needed.",
                "resume_unsupported",
            )
        if doc["status"] == "running":
            title = doc.get("title") or doc.get("task_id")
            return _fail(
                f"{title} is still running. Use cancel_task if you need to stop this run.",
                "work_still_running",
            )
        worker_kind = lineage_worker_kind(doc)
        if not worker_ready(worker_kind):
            return _dispatch_result(
                ok=False,
                error_code="background_credentials_unavailable",
                error_message=missing_credential_message(worker_kind),
                message="Resume not started: background-agent credentials are unavailable.",
            )

        cwd = _normalize_agent_cwd(doc.get("cwd"))
        if not cwd:
            return _fail("This work has no project folder to resume in.", "cwd_required")
        max_turns = doc.get("max_turns", 40)
        max_budget_usd = doc.get("max_budget_usd", 1.0)
        resume_session_id = doc.get("session_id")
        work_id = str(doc.get("work_id") or generate_id())
        title = str(doc.get("title") or "").strip() or "Untitled"

        _pb = _PromptBuilder()
        connected_apps = await _get_connected_composio_apps()
        mcp_servers, system_prompt = await asyncio.gather(
            _resolve_tools_for_dispatch(connected_apps),
            _pb.build_subprocess_prompt(settings.DEFAULT_USER_ID, cwd, ""),
        )

        extra_doc = {
            "mcp_servers": mcp_servers,
            "progress_summary": "Resuming…",
            "work_id": work_id,
            "title": title,
            "open": True,
            "session_id": resume_session_id,
            "worker_kind": worker_kind,
        }
        if doc.get("ref"):
            extra_doc["ref"] = doc.get("ref")

        result = await self._prepare_task(
            prompt=instruction, cwd=cwd, mode="code", max_turns=max_turns,
            max_budget_usd=max_budget_usd, source="resume",
            trigger_ref=doc.get("trigger_ref"), depth=0,
            extra_doc=extra_doc,
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
                    prompt=instruction,
                    cwd=cwd,
                    max_turns=max_turns,
                    mcp_servers=mcp_servers,
                    system_prompt=system_prompt,
                    resume_session_id=resume_session_id,
                    max_budget_usd=max_budget_usd,
                    worker_kind=worker_kind,
                    title=title,
                )

        self._spawn_task(new_task_id, _run(), "resume")
        return _dispatch_result(
            ok=True,
            task_id=new_task_id,
            work_id=work_id,
            mode="code",
            message=f"Continuing {title}.",
        )

    @tool
    async def inspect(self, target: str, path: str | None = None) -> str:
        """
        Open named code-mode work in the editor so the user can review files.
        target is a title, ref, work_id, or task_id. Pass path to open one file in the project.
        Does not load the transcript into this turn.
        """
        doc = await self._lookup_work(target)
        if isinstance(doc, CapabilityErrorDetail):
            return doc
        title = str(doc.get("title") or doc.get("task_id") or "Untitled")
        if doc.get("mode") == "jarvis":
            return _fail(
                f"{title} is jarvis-mode work and has no coding session to open.",
                "inspect_unsupported",
            )
        cwd = _normalize_agent_cwd(doc.get("cwd"))
        if not cwd:
            return _fail(f"{title} has no project folder to open.", "session_missing")
        worker = worker_for_kind(lineage_worker_kind(doc))
        via = getattr(worker, "inspect_via", "terminal")
        file_path = str(path or "").strip()
        if file_path:
            resolved = path_under_cwd(file_path, cwd)
            if not resolved:
                return _fail("That file is outside this project.", "inspect_path_denied")
            targets = [resolved]
            opened = os.path.basename(resolved)
        else:
            targets = [cwd]
            opened = title

        if file_path or via == "ide":
            if via == "ide":
                return await self._open_in_editor(worker, targets, opened)
            if sys.platform == "darwin":
                return await self._launch_inspect(
                    ["open", *targets],
                    fail_detail=f"Could not open {opened}.",
                    success=f"Opened {opened}.",
                )
            return _fail(f"Open {opened} from the project folder.", "inspect_unsupported")

        if doc.get("status") == "running":
            return _fail(
                f"{title} is still running. Inspect after it finishes.",
                "work_still_running",
            )
        session_id = str(doc.get("session_id") or "").strip()
        if not session_id:
            return _fail(f"{title} has no session to open.", "session_missing")
        binary = next((name for name in worker.inspect_binaries if shutil.which(name)), None)
        command = inspect_resume_command(cwd, session_id, binary or worker.inspect_binaries[0])
        if binary is None:
            return _fail(
                f"Install the {worker.inspect_label} CLI to inspect this work. Then run: {command}",
                "inspect_cli_missing",
            )
        if sys.platform != "darwin":
            return _fail(f"Open a terminal and run: {command}", "inspect_unsupported")
        return await self._launch_inspect(
            inspect_launch_argv(cwd, session_id, binary=binary),
            fail_detail=f"Could not open Terminal. Run: {command}",
            success=f"Opened {title} in {worker.inspect_label}.",
        )

    async def _open_in_editor(self, worker: Any, targets: list[str], opened: str) -> str:
        binary = next((name for name in worker.inspect_binaries if shutil.which(name)), None)
        if binary:
            argv = inspect_ide_argv(binary, targets)
        elif sys.platform == "darwin":
            argv = inspect_macos_open_argv(getattr(worker, "inspect_app", worker.inspect_label), targets)
        else:
            return _fail(
                f"Install {worker.inspect_label} to open this work.",
                "inspect_cli_missing",
            )
        return await self._launch_inspect(
            argv,
            fail_detail=f"Could not open {opened} in {worker.inspect_label}.",
            success=f"Opened {opened} in {worker.inspect_label}.",
        )

    async def _launch_inspect(self, argv: list[str], *, fail_detail: str, success: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            return _fail(detail or fail_detail, "inspect_failed")
        return success

    @tool
    async def get_status(self, target: str | None = None) -> str:
        """
        Status for named work, or the recency roster when target is omitted.
        target is a title, ref, work_id, or task_id. Do not use recall() to find this.
        Completed open work is still open — resume to continue or inspect to read it.
        """
        if not target:
            text = await load_open_roster(settings.DEFAULT_USER_ID)
            return text or "No open work."

        doc = await self._lookup_work(target)
        if isinstance(doc, CapabilityErrorDetail):
            return doc
        return format_status(doc)

    @tool
    async def get_result(self, target: str | None = None) -> str:
        """
        Retrieve the stored result for the last run of named work.
        target is a title, ref, work_id, or task_id. Omit for the latest completed open work.
        Do not dispatch again to recover a result. If they want more done, resume.
        If they want to read the job, inspect — do not narrate the transcript.
        """
        col = mongodb.get_collection("background_tasks")
        projection = {
            "_id": 0,
            "task_id": 1,
            "work_id": 1,
            "title": 1,
            "status": 1,
            "result": 1,
            "progress_summary": 1,
            "artifacts": 1,
            "open": 1,
        }
        if target:
            doc = await self._lookup_work(target)
            if isinstance(doc, CapabilityErrorDetail):
                return json.dumps(
                    {
                        "ok": False,
                        "task_id": None,
                        "work_id": None,
                        "status": None,
                        "result": None,
                        "artifacts": [],
                        "error_code": doc.code,
                        "error_message": doc.message,
                        "message": doc.message,
                    }
                )
            full = await col.find_one(
                {"task_id": doc["task_id"], "owner_id": settings.DEFAULT_USER_ID},
                projection,
            )
            doc = full or doc
        else:
            open_docs = await list_open_work(settings.DEFAULT_USER_ID)
            doc = next(
                (item for item in open_docs if item.get("status") in {"completed", "failed"}),
                None,
            )
            if doc:
                full = await col.find_one(
                    {"task_id": doc["task_id"], "owner_id": settings.DEFAULT_USER_ID},
                    projection,
                )
                doc = full or doc

        if not doc:
            return json.dumps(
                {
                    "ok": False,
                    "task_id": None,
                    "work_id": None,
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
                    "work_id": doc.get("work_id"),
                    "status": status,
                    "result": None,
                    "artifacts": [],
                    "error_code": "task_still_running",
                    "error_message": "The task is still running; use get_status() for progress.",
                    "message": "Task is still running.",
                }
            )

        result = doc.get("result") or doc.get("progress_summary") or ""
        still_open = doc.get("open") is True
        return json.dumps(
            {
                "ok": True,
                "task_id": doc.get("task_id"),
                "work_id": doc.get("work_id"),
                "title": doc.get("title"),
                "open": still_open,
                "status": status,
                "result": result,
                "artifacts": doc.get("artifacts") or [],
                "error_code": None,
                "error_message": None,
                "message": (
                    "Last run result. Still open — resume to continue."
                    if still_open
                    else "Task result found."
                ),
            }
        )

    @tool
    async def list_tasks(self, status: str | None = None) -> list[dict]:
        """
        List open named work by default. Pass status to list runs: running, completed, failed, or cancelled.
        """
        if not status:
            return [compact_task(doc) for doc in await list_open_work(settings.DEFAULT_USER_ID)]

        col = mongodb.get_collection("background_tasks")
        cursor = col.find(
            {"owner_id": settings.DEFAULT_USER_ID, "status": status},
            {"events": 0, "trace": 0, "mcp_servers": 0, "_id": 0},
        ).sort("created_at", -1).limit(20)
        docs = await cursor.to_list(length=20)
        return [compact_task(doc) for doc in docs]

    @tool
    async def cancel_task(self, target: str) -> str:
        """Cancel a running run. Does not close the named work."""
        doc = await self._lookup_work(target)
        if isinstance(doc, CapabilityErrorDetail):
            return doc
        task_id = str(doc.get("task_id") or "")
        task = self._running_tasks.get(task_id)
        if not task:
            if doc.get("status") != "running":
                return _fail(
                    f"{doc.get('title') or task_id} is not running (status={doc.get('status')}).",
                    "work_not_running",
                )
            return _fail(f"Task {task_id} is not running in this process.", "work_not_running")
        task.cancel()
        return f"Cancellation requested for {doc.get('title') or task_id}."

    @tool
    async def close(self, target: str) -> str:
        """
        Forget named work so it leaves the recency roster. Optional — finishing a run does not require this.
        Use when the user is done or says not to bring it up. Does not cancel a running run.
        """
        doc = await self._lookup_work(target)
        if isinstance(doc, CapabilityErrorDetail):
            return doc
        work_id = doc.get("work_id")
        col = mongodb.get_collection("background_tasks")
        now = datetime.now(timezone.utc)
        filt: dict[str, Any] = {"owner_id": settings.DEFAULT_USER_ID}
        if work_id:
            filt["work_id"] = work_id
        else:
            filt["task_id"] = doc.get("task_id")
        result = await col.update_many(
            filt,
            {"$set": {"open": False, "expires_at": ttl_at(now)}},
        )
        title = doc.get("title") or doc.get("task_id")
        count = int(getattr(result, "modified_count", 0) or 0)
        return f"Closed {title} ({count} run(s))."

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
        title: str,
        ref: str | None = None,
        max_turns: int = 40,
        max_budget_usd: float = 1.0,
        source: str = "voice",
        trigger_ref: str | None = None,
        depth: int = 0,
        worker_kind: str | None = None,
    ) -> str:
        """Shared implementation for dispatch() and automation-triggered calls."""
        work_id = generate_id()
        resolved_kind = worker_kind or default_worker_kind()
        if resolved_kind is None:
            return _dispatch_result(
                ok=False,
                error_code="background_credentials_unavailable",
                error_message=missing_credential_message(),
                message="Task not started: background-agent credentials are unavailable.",
            )
        _pb = _PromptBuilder()
        connected_apps, conv_context = await asyncio.gather(
            _get_connected_composio_apps(),
            _PromptBuilder.build_conversation_context(settings.DEFAULT_USER_ID),
        )
        mcp_servers, system_prompt = await asyncio.gather(
            _resolve_tools_for_dispatch(connected_apps),
            _pb.build_subprocess_prompt(settings.DEFAULT_USER_ID, cwd, conv_context),
        )

        extra_doc: dict[str, Any] = {
            "mcp_servers": mcp_servers,
            "work_id": work_id,
            "title": title,
            "open": True,
            "worker_kind": resolved_kind,
        }
        if ref:
            extra_doc["ref"] = ref

        result = await self._prepare_task(
            prompt=prompt, cwd=cwd, mode="code", max_turns=max_turns,
            max_budget_usd=max_budget_usd, source=source, trigger_ref=trigger_ref,
            depth=depth, extra_doc=extra_doc,
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
                    worker_kind=resolved_kind,
                    title=title,
                )

        self._spawn_task(task_id, _run(), source)
        logger.info("Dispatched agent task %s work_id=%s (source=%s, depth=%d)", task_id, work_id, source, depth)
        return _dispatch_result(
            ok=True,
            task_id=task_id,
            work_id=work_id,
            mode="code",
            message=f"I'll take {title} and let you know when it's done.",
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
                    from core.tool_router import tool_router

                    bg_context = await build_background_context(owner_id, cwd)

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
                    await _cancel_task(task_id, owner_id)
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

