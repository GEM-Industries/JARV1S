"""V0 habits plugin: cue-based habit creation, check-ins, and logging."""

from __future__ import annotations

from typing import Literal

from core.context import get_ctx, get_owner_id, get_timezone
from core.decorators import tool
from core.plugins.result import ToolResult
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.plugins.ui import content_envelope, receipt_envelope
from core.scheduling import is_valid
from core.plugins.capabilities import CapabilityErrorDetail

from . import store
from .models import HabitCheckinKind, HabitCheckinPlan, HabitLogDetails, HabitStatus
from .triggers import default_checkin_message, schedule_checkin



def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


class HabitsPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="habits",
        version="0.1.0",
        description="Cue-based habit check-ins and voice-friendly habit logging.",
        utterances=[
            "help me build a habit",
            "track my reading habit",
            "log that I did my habit",
            "I missed my habit today",
            "skip my habit today",
            "how am I doing with my habit",
            "what is set up around my habit",
            "remind me to check in on my habit",
            "log my bedtime for my sleep habit",
        ],
    )

    @tool
    async def create_habit(
        self,
        name: str,
        behavior: str,
        cue: str | None = None,
        minimum_version: str | None = None,
        desired_frequency: str | None = None,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Create a cue-based habit plan when the user wants to build, start, or track a recurring behavior.
        behavior is the target routine; cue is the user's anchor; minimum_version is the smallest acceptable win.
        If the same request includes a check-in time, create the habit first, then call schedule_habit_checkin.
        """
        if not name.strip() or not behavior.strip():
            return _fail("Habit name and behavior are required.")

        try:
            habit = await store.create_habit(
                owner_id=get_owner_id(),
                name=name,
                behavior=behavior,
                cue=cue,
                minimum_version=minimum_version,
                desired_frequency=desired_frequency,
            )
        except ValueError:
            return _fail(f"Habit '{name.strip()}' already exists.")
        line = _habit_label(habit.name, habit.cue)
        return ToolResult(
            content=f"Created habit '{habit.name}'.",
            ui=[receipt_envelope("Habit Created", line, sublabel=_habit_sublabel(habit))],
        )

    @tool
    async def log_habit(
        self,
        habit_id: str,
        status: Literal["done", "missed", "skipped"] = "done",
        note: str | None = None,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Log a habit outcome. Use done for success, missed for intended-but-not-done, skipped for an intentional pass.
        For missed, include the user's reason in note if they gave one; do not force a reason.
        Call get_habit_status first if you need to resolve the habit id from the habit name.
        """
        try:
            habit, log = await store.log_habit(
                owner_id=get_owner_id(),
                habit_id=habit_id,
                status=status,
                note=note,
                source=_log_source(),
            )
        except ValueError:
            return _fail("Habit not found. Call get_habit_status() to resolve the habit id.")

        return ToolResult(
            content=f"Logged {habit.name} as {log.status}.",
            ui=[
                receipt_envelope(
                    "Habit Logged",
                    f"{habit.name} · {log.status}",
                    sublabel=note,
                )
            ],
        )

    @tool
    async def log_habit_by_name(
        self,
        name: str,
        status: Literal["done", "missed", "skipped"] = "done",
        note: str | None = None,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Log a habit by name. Use for natural phrases or replies to a habit check-in where the prior prompt named the habit.
        Map simple confirmations to done, intended-but-not-done answers to missed, and intentional passes to skipped.
        For missed, include the user's reason in note if they gave one; do not ask more than one follow-up.
        """
        try:
            habit, log = await store.log_habit_by_name(
                owner_id=get_owner_id(),
                name=name,
                status=status,
                note=note,
                source=_log_source(),
            )
        except ValueError:
            return _fail("Habit not found. Call get_habit_status() to see active habits.")

        return ToolResult(
            content=f"Logged {habit.name} as {log.status}.",
            ui=[
                receipt_envelope(
                    "Habit Logged",
                    f"{habit.name} · {log.status}",
                    sublabel=note,
                )
            ],
        )

    @tool
    async def log_measured_habit_by_name(
        self,
        name: str,
        metric: str,
        observed_value: str,
        status: Literal["done", "missed", "skipped"] = "done",
        target: str | None = None,
        delta: str | None = None,
        unit: str | None = None,
        note: str | None = None,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Log a habit with measured evidence, such as an observed value versus a target. Keep voice capture low-friction; do not ask form-style follow-ups.
        Use observed_value for what the user gave, target/delta only when known, and leave unknown fields omitted.
        """
        if not metric.strip() or not observed_value.strip():
            return _fail("metric and observed_value are required.")

        details = HabitLogDetails(
            metric=metric.strip(),
            observed_value=observed_value.strip(),
            target=_clean_optional(target),
            delta=_clean_optional(delta),
            unit=_clean_optional(unit),
        )
        try:
            habit, log = await store.log_habit_by_name(
                owner_id=get_owner_id(),
                name=name,
                status=status,
                note=note,
                details=details,
                source=_log_source(),
            )
        except ValueError:
            return _fail("Habit not found. Call get_habit_status() to see active habits.")

        return ToolResult(
            content=f"Logged {habit.name} {details.metric} as {details.observed_value}.",
            ui=[
                receipt_envelope(
                    "Habit Evidence Logged",
                    f"{habit.name} · {details.metric}: {details.observed_value}",
                    sublabel=_details_sublabel(log.details, note),
                )
            ],
        )

    @tool
    async def get_habit_status(self, habit_id: str | None = None, days: int = 7) -> ToolResult:
        """
        Show recent habit progress for one habit or all active habits. habit_id may be the stable id or exact habit name.
        Use returned habit_id values for follow-up tools; never read habit ids aloud.
        """
        statuses = await store.get_habit_statuses(
            owner_id=get_owner_id(),
            habit_id=habit_id,
            days=days,
        )
        if not statuses:
            content = "No active habits found."
            return ToolResult(
                content=content,
                ui=[content_envelope("Habit Status", [{"type": "markdown", "content": content}])],
            )

        content = _status_content(statuses)
        return ToolResult(
            content=content,
            ui=[content_envelope("Habit Status", _status_sections(statuses))],
        )

    @tool
    async def list_habit_checkins(self, habit_id: str) -> ToolResult | CapabilityErrorDetail:
        """
        List habit-owned check-ins linked by habit_id or exact habit name. Use this instead of scheduler.get_alerts when asking what is set up around a habit.
        """
        owner_id = get_owner_id()
        habit = await store.resolve_habit(owner_id, habit_id)
        if habit is None:
            return _fail("Habit not found. Call get_habit_status() to see active habit names and ids.")

        checkins = await store.list_habit_checkins(owner_id=owner_id, habit_id=habit.id)
        content = _checkins_content(habit.name, checkins)
        return ToolResult(
            content=content,
            ui=[content_envelope("Habit Check-Ins", _checkins_sections(habit.name, checkins))],
        )

    @tool
    async def get_habit_setup(self, habit_id: str, days: int = 7) -> ToolResult | CapabilityErrorDetail:
        """
        Show one habit's metadata, recent logs, and linked habit-owned check-ins. habit_id may be the stable id or exact habit name.
        """
        setup = await store.get_habit_setup(
            owner_id=get_owner_id(),
            habit_id=habit_id,
            days=days,
        )
        if setup is None:
            return _fail("Habit not found. Call get_habit_status() to see active habit names and ids.")

        content = _setup_content(setup)
        return ToolResult(
            content=content,
            ui=[content_envelope("Habit Setup", _setup_sections(setup))],
        )

    @tool
    async def schedule_habit_checkin(
        self,
        habit_id: str,
        when: str,
        message: str | None = None,
        recurrence: str | None = None,
        checkin_kind: HabitCheckinKind = "habit_checkin",
        instructions: str | None = None,
        decision: Literal["tell", "offer"] = "tell",
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Schedule a habit-owned check-in plan and materialize its trigger. recurrence accepts daily, weekdays, weekends, weekly, every Xh, every Xm.
        Use after create_habit when the user asks to be checked in, prompted, reviewed, or reminded about the habit.
        Use checkin_kind by habit-loop role: habit_checkin asks for an outcome, cue_prompt prompts before the cue, review asks after. Do not use scheduler.remind for habit-owned prompts.
        Use decision="offer" with instructions for conditional prompts that should be evaluated before speaking; otherwise leave decision as tell.
        For offer instructions, state the decision boundary: what evidence means ask now, what means defer until later, and what means the check-in is already handled or no longer useful.
        Use replace_habit_checkin for edits or rescheduling; do not delete and recreate an existing plan.
        To replace a generic reminder, create the habit check-in first, then cancel the old scheduler alert only if the user wants no duplicate.
        """
        instructions = _clean_optional(instructions)
        message = _clean_optional(message)
        if recurrence and not is_valid(recurrence):
            return _fail(f"Invalid recurrence '{recurrence}'. Use: daily, weekdays, weekends, weekly, every Xh, every Xm.")
        if decision == "offer" and not instructions:
            return _fail('decision="offer" requires instructions.')

        owner_id = get_owner_id()
        habit = await store.resolve_habit(owner_id, habit_id)
        if habit is None:
            return _fail("Habit not found. Call get_habit_status() to see active habit names and ids.")

        plan = HabitCheckinPlan(
            owner_id=owner_id,
            habit_id=habit.id,
            checkin_kind=checkin_kind,
            message=message or default_checkin_message(habit, checkin_kind=checkin_kind),
            when=when.strip(),
            timezone=get_timezone(),
            recurrence=recurrence.lower().strip() if recurrence else None,
            instructions=instructions,
            decision=decision,
        )

        try:
            scheduled = await schedule_checkin(
                owner_id=owner_id,
                timezone_name=plan.timezone,
                habit=habit,
                when=plan.when,
                message=plan.message,
                recurrence=plan.recurrence,
                checkin_kind=plan.checkin_kind,
                plan_id=plan.id,
                instructions=plan.instructions,
                decision=plan.decision,
            )
        except ValueError as exc:
            return _fail(
                f"Could not parse when={when!r}. {exc}. "
                "Use formats like '30m', '17:00', '5pm', 'today 17:00', or 'Friday at 7pm'."
            )
        plan.rule_id = scheduled.rule_id
        plan.initial_instance_id = scheduled.instance_id
        await store.save_habit_checkin_plan(plan)

        label = "Recurring Habit Check-In" if scheduled.recurrence else "Habit Check-In"
        suffix = f"{checkin_kind} · {scheduled.when_label}"
        if scheduled.recurrence:
            suffix = f"{checkin_kind} · {scheduled.recurrence} · {scheduled.when_label}"
        return ToolResult(
            content=f"Scheduled habit check-in for {habit.name} at {scheduled.when_label}.",
            ui=[receipt_envelope(label, f"{habit.name} · {suffix}")],
        )

    @tool
    async def replace_habit_checkin(
        self,
        plan_id: str,
        when: str | None = None,
        message: str | None = None,
        recurrence: str | None = None,
        checkin_kind: HabitCheckinKind | None = None,
        instructions: str | None = None,
        decision: Literal["tell", "offer"] | None = None,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Edit or reschedule an existing habit check-in plan and its linked trigger in one operation.
        Preserve omitted fields. Use plan_id from get_habit_status or setups.find; do not delete and recreate for edits.
        """
        owner_id = get_owner_id()
        current = await store.get_checkin_plan(owner_id, plan_id)
        if current is None:
            return _fail("Habit check-in plan not found. Use plan_id from get_habit_status or setups.find.")
        habit = await store.get_habit(owner_id, current.habit_id)
        if habit is None:
            return _fail("Linked habit not found; the check-in cannot be updated.")

        next_recurrence = current.recurrence
        if recurrence is not None:
            next_recurrence = recurrence.lower().strip()
            if not is_valid(next_recurrence):
                return _fail(f"Invalid recurrence '{recurrence}'. Use: daily, weekdays, weekends, weekly, every Xh, every Xm.")

        next_instructions = (
            _clean_optional(instructions)
            if instructions is not None
            else current.instructions
        )
        next_decision = decision or current.decision
        if next_decision == "offer" and not next_instructions:
            return _fail('decision="offer" requires instructions.')

        next_when = _clean_optional(when) or current.when
        updated = current.model_copy(update={
            "when": next_when,
            "timezone": get_timezone() if when is not None else current.timezone,
            "message": _clean_optional(message) or current.message,
            "recurrence": next_recurrence,
            "checkin_kind": checkin_kind or current.checkin_kind,
            "instructions": next_instructions,
            "decision": next_decision,
        })
        try:
            updated = await store.replace_checkin_plan(updated, habit=habit)
        except ValueError as exc:
            text = str(exc)
            if "parse" in text.lower() or "when" in text.lower():
                return _fail(
                    f"Could not parse when={when!r}. {exc}. "
                    "Use formats like '30m', '17:00', '5pm', 'today 17:00', or 'Friday at 7pm'."
                )
            return _fail(f"Could not update habit check-in. {exc}.")

        cadence = f"{updated.recurrence} · " if updated.recurrence else ""
        return ToolResult(
            content=f"Updated habit check-in for {habit.name}.",
            ui=[receipt_envelope(
                "Habit Check-In Updated",
                f"{habit.name} · {cadence}{updated.when}",
            )],
        )

    @tool
    async def delete_habit_checkin(self, plan_id: str) -> str:
        """Delete one habit-owned check-in plan and its linked trigger artifacts."""
        try:
            await store.delete_checkin_plan(get_owner_id(), plan_id)
        except ValueError:
            return _fail("Habit check-in plan not found.")
        return "Deleted habit check-in plan."

    @tool
    async def pause_habit_checkin(self, plan_id: str) -> str:
        """Pause one habit-owned check-in plan without deleting it."""
        try:
            await store.pause_checkin_plan(get_owner_id(), plan_id)
        except ValueError:
            return _fail("Habit check-in plan not found.")
        return "Paused habit check-in plan."

    @tool
    async def resume_habit_checkin(self, plan_id: str) -> str:
        """Resume a paused habit-owned check-in plan."""
        try:
            await store.resume_checkin_plan(get_owner_id(), plan_id)
        except ValueError:
            return _fail("Habit check-in plan not found.")
        return "Resumed habit check-in plan."


def _habit_label(name: str, cue: str | None) -> str:
    return f"{name} · {cue}" if cue else name


def _habit_sublabel(habit) -> str | None:
    parts = []
    if habit.minimum_version:
        parts.append(f"Minimum: {habit.minimum_version}")
    if habit.desired_frequency:
        parts.append(habit.desired_frequency)
    return " · ".join(parts) if parts else None


def _log_source() -> Literal["voice", "text", "ui", "system"]:
    source = get_ctx().get("invocation_source")
    return "ui" if source == "ui_action" else "voice"


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _status_content(statuses: list[HabitStatus]) -> str:
    parts = []
    for status in statuses:
        summary = (
            f"{status.name} (habit_id={status.habit_id}): "
            f"{status.done} done, {status.missed} missed, "
            f"{status.skipped} skipped over {status.days} days"
        )
        if status.last_status:
            summary += f"; last logged {status.last_status}"
        measured = _latest_measured_summary(status)
        if measured:
            summary += f"; latest {measured}"
        if status.suggested_adjustment:
            summary += f". {status.suggested_adjustment}"
        parts.append(summary)
    return "\n".join(parts)


def _status_sections(statuses: list[HabitStatus]) -> list[dict]:
    sections: list[dict] = []
    for status in statuses:
        pairs = {
            "Habit": status.name,
            "Habit ID": status.habit_id,
            "Behavior": status.behavior,
            "Window": f"{status.days} days",
            "Done": str(status.done),
            "Missed": str(status.missed),
            "Skipped": str(status.skipped),
        }
        if status.cue:
            pairs["Cue"] = status.cue
        if status.minimum_version:
            pairs["Minimum"] = status.minimum_version
        if status.last_status:
            pairs["Last"] = status.last_status
        sections.append({"type": "kv", "pairs": pairs})
        measured = [
            _log_details_line(log.details)
            for log in status.recent_logs
            if log.details is not None
        ]
        if measured:
            sections.append({"type": "list", "items": measured})
        if status.suggested_adjustment:
            sections.append({"type": "markdown", "content": status.suggested_adjustment})
    return sections


def _latest_measured_summary(status: HabitStatus) -> str | None:
    for log in status.recent_logs:
        if log.details:
            return _log_details_line(log.details)
    return None


def _log_details_line(details: HabitLogDetails) -> str:
    parts = [f"{details.metric}: {details.observed_value}"]
    if details.target:
        parts.append(f"target {details.target}")
    if details.delta:
        parts.append(f"delta {details.delta}")
    if details.unit:
        parts.append(details.unit)
    return " · ".join(parts)


def _details_sublabel(details: HabitLogDetails | None, note: str | None) -> str | None:
    parts = []
    if details:
        if details.target:
            parts.append(f"Target: {details.target}")
        if details.delta:
            parts.append(f"Delta: {details.delta}")
    if note:
        parts.append(note)
    return " · ".join(parts) if parts else None


def _checkins_content(name: str, checkins) -> str:
    if not checkins:
        return f"{name} has no linked habit check-ins."
    lines = [f"{name} has {len(checkins)} linked habit check-in(s):"]
    lines.extend(
        f"{checkin.checkin_kind}: {checkin.status}"
        + (f", {checkin.recurrence}" if checkin.recurrence else "")
        for checkin in checkins
    )
    return "\n".join(lines)


def _checkins_sections(name: str, checkins) -> list[dict]:
    if not checkins:
        return [{"type": "markdown", "content": f"{name} has no linked habit check-ins."}]
    sections: list[dict] = []
    for checkin in checkins:
        pairs = {
            "Kind": checkin.checkin_kind,
            "Message": checkin.message,
            "Status": checkin.status,
            "Scope": checkin.scope,
        }
        if checkin.recurrence:
            pairs["Recurrence"] = checkin.recurrence
        if checkin.rule_id:
            pairs["Rule"] = checkin.rule_id
        if checkin.instance_id:
            pairs["Instance"] = checkin.instance_id
        sections.append({"type": "kv", "pairs": pairs})
    return sections


def _setup_content(setup) -> str:
    status = setup.recent_status
    content = _status_content([status])
    if setup.checkins:
        kinds = ", ".join(checkin.checkin_kind for checkin in setup.checkins)
        content += f"\nLinked check-ins: {len(setup.checkins)} ({kinds})."
    else:
        content += "\nNo linked habit check-ins."
    return content


def _setup_sections(setup) -> list[dict]:
    habit = setup.habit
    pairs = {
        "Habit": habit.name,
        "Behavior": habit.behavior,
        "Active": str(habit.active),
    }
    if habit.cue:
        pairs["Cue"] = habit.cue
    if habit.minimum_version:
        pairs["Minimum"] = habit.minimum_version
    if habit.desired_frequency:
        pairs["Frequency"] = habit.desired_frequency

    sections = [{"type": "kv", "pairs": pairs}]
    sections.extend(_status_sections([setup.recent_status]))
    sections.extend(_checkins_sections(habit.name, setup.checkins))
    return sections
