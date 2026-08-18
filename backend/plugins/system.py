"""
System Control Plugin for JARV1S.

Pre-built tools for common operations (volume, app launching, status, repeat_last).
Shell execution via exec() with allow/ask/deny policy + approve_pending / deny_pending.
Machine health diagnostics via diagnostics() and top_processes().
"""

import asyncio
import inspect
import json
import logging
import os
import platform
import re
import secrets
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import psutil
from core.context import get_connection_id, get_owner_id
from core.decorators import tool
from core.id import generate_id
from core.plugins.consent import cancel_pending, execute_pending, require_consent
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.result import ToolResult
from core.plugins.types import JarvisPlugin, PluginMetadata, UIEnvelope, WidgetLayout, WidgetSize
from plugins.system_exec_policy import ExecVerdict, classify_exec
from pydantic import BaseModel, Field
from services.events import Event, EventType, event_bus


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


logger = logging.getLogger(__name__)

# --- Module-level state ---

_start_time = time.time()

# Lazy per-tool search index used by search_tools().
_tool_index_signature: str | None = None


@dataclass(frozen=True)
class _ToolIndexEntry:
    plugin: str
    description: str
    signature: str
    required_parameter_names: list[str]
    parameters: dict[str, str]
    search_text: str
    vector: list[float] | None = None


_tool_index: dict[str, _ToolIndexEntry] = {}

_TOKEN_RE = re.compile(r"[a-z0-9]+")

EXEC_TIMEOUT_SECS = 30
MAX_OUTPUT_LINES = 80
MAX_OUTPUT_BYTES = 50 * 1024  # OpenCode-aligned inline budget
TAIL_RATIO = 0.75  # keep 75% of budget for the tail (where errors/results appear)
TOOL_OUTPUT_DIR = Path.home() / ".jarvis" / "tool-output"


class DiscoveredTool(BaseModel):
    fqn: str
    name: str
    plugin: str
    description: str
    parameters: dict[str, str] = Field(default_factory=dict)


class ToolSearchResult(BaseModel):
    tools: list[DiscoveredTool] = Field(default_factory=list)


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    truncated: bool = False
    spill_path: str | None = None


def _display_spill_path(path: Path) -> str:
    """Prefer ~/... for agent-facing paths."""
    try:
        return f"~/{path.resolve().relative_to(Path.home().resolve())}"
    except ValueError:
        return str(path)


def _spill_exec_output(full_text: str) -> str | None:
    """Best-effort write of full exec output. Returns display path or None on failure."""
    try:
        directory = TOOL_OUTPUT_DIR
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / f"exec_{secrets.token_hex(6)}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as file:
            file.write(full_text)
        return _display_spill_path(path)
    except OSError as e:
        logger.warning("Failed to spill exec output: %s", e)
        return None


def _format_exec_result(result: ExecResult) -> str:
    """Plain-text rendering for the model: stdout, stderr, exit footer, spill hint."""
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(result.stderr if not result.stdout else f"[stderr]\n{result.stderr}")
    body = "\n".join(parts).strip()
    if result.exit_code != 0:
        body = f"{body}\n[exit {result.exit_code}]".strip()
    if not body:
        body = f"[completed, exit {result.exit_code}]"
    if result.spill_path:
        body = (
            f"{body}\n\n"
            f"Full output saved to: {result.spill_path}\n"
            "Use jarvis.files.grep(pattern=..., path=...) or "
            "jarvis.files.read(path=..., offset=...) to inspect."
        )
    elif result.truncated:
        body = f"{body}\n\n[output truncated; could not save full output to disk]"
    return body


# --- Internal helpers ---

def _truncate(text: str, max_lines: int = MAX_OUTPUT_LINES) -> tuple[str, bool]:
    """Head+tail truncation: keeps the first N and last M lines, drops the middle."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    tail_count = int(max_lines * TAIL_RATIO)
    head_count = max_lines - tail_count
    omitted = len(lines) - head_count - tail_count
    return "\n".join(
        lines[:head_count]
        + [f"\n[... {omitted} lines truncated ...]\n"]
        + lines[-tail_count:]
    ), True


def _truncate_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False

    tail_bytes = int(max_bytes * TAIL_RATIO)
    head_bytes = max_bytes - tail_bytes
    omitted = len(encoded) - max_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore")
    return f"{head}\n[... {omitted} bytes truncated ...]\n{tail}", True


def _prepare_exec_streams(stdout_raw: str, stderr_raw: str) -> ExecResult:
    """Truncate streams and spill full output when over line or byte budget."""
    meaningful = [
        line for line in stderr_raw.splitlines()
        if "Operation not permitted" not in line
    ]
    stderr_full = "\n".join(meaningful)

    full_parts = [stdout_raw] if stdout_raw else []
    if stderr_full:
        full_parts.append(f"[stderr]\n{stderr_full}" if stdout_raw else stderr_full)
    full_text = "\n".join(full_parts)
    over_bytes = len(full_text.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES

    stdout, truncated_out = _truncate(stdout_raw)
    stderr, truncated_err = _truncate(stderr_full) if stderr_full else ("", False)
    preview_bytes = len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))
    if preview_bytes > MAX_OUTPUT_BYTES:
        if not stderr:
            stdout, clipped_out = _truncate_bytes(stdout, MAX_OUTPUT_BYTES)
            truncated_out = truncated_out or clipped_out
        elif not stdout:
            stderr, clipped_err = _truncate_bytes(stderr, MAX_OUTPUT_BYTES)
            truncated_err = truncated_err or clipped_err
        else:
            stdout_size = len(stdout.encode("utf-8"))
            stderr_size = len(stderr.encode("utf-8"))
            half = MAX_OUTPUT_BYTES // 2
            if stdout_size <= half:
                stdout_budget = stdout_size
                stderr_budget = MAX_OUTPUT_BYTES - stdout_budget
            elif stderr_size <= half:
                stderr_budget = stderr_size
                stdout_budget = MAX_OUTPUT_BYTES - stderr_budget
            else:
                stdout_budget = half
                stderr_budget = MAX_OUTPUT_BYTES - half
            stdout, clipped_out = _truncate_bytes(stdout, stdout_budget)
            stderr, clipped_err = _truncate_bytes(stderr, stderr_budget)
            truncated_out = truncated_out or clipped_out
            truncated_err = truncated_err or clipped_err
    truncated = truncated_out or truncated_err or over_bytes

    spill_path = None
    if truncated and full_text:
        spill_path = _spill_exec_output(full_text)

    return ExecResult(
        stdout=stdout,
        stderr=stderr,
        truncated=truncated,
        spill_path=spill_path,
    )


def _tool_signature() -> str:
    from core.plugins.registry import registry

    names = sorted(
        f"{definition.plugin}:{definition.name}:{definition.visible_signature_str}"
        for definition in registry.iter_capabilities(enabled_only=True)
    )
    return str(hash(tuple(names)))


def _visible_param_names_from_sig(sig: inspect.Signature) -> list[str]:
    return [p.name for p in sig.parameters.values()]


def _visible_required_param_names_from_sig(sig: inspect.Signature) -> list[str]:
    return [
        p.name
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
    ]


def _extract_param_docs(doc: str, param_names: list[str]) -> dict[str, str]:
    """Extract compact `Args:` docs for the visible parameters only."""
    if not doc or not param_names:
        return {}

    wanted = set(param_names)
    lines = inspect.cleandoc(doc).splitlines()
    in_args = False
    current: str | None = None
    result: dict[str, str] = {}

    for line in lines:
        stripped = line.strip()
        if not in_args:
            if stripped.lower() in {"args:", "arguments:", "parameters:"}:
                in_args = True
            continue

        if not stripped:
            current = None
            continue

        match = re.match(r"^([A-Za-z_]\w*)\s*:\s*(.*)$", stripped)
        if match:
            name, description = match.groups()
            if name in wanted:
                result[name] = description.strip()
                current = name
            else:
                current = None
            continue

        if current and (line.startswith(" ") or line.startswith("\t")):
            result[current] = f"{result[current]} {stripped}".strip()
            continue

        # A new section started.
        if stripped.endswith(":"):
            break

    return result


def _search_tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.replace("_", " ").lower()))


def _tool_card(fqn: str, entry: _ToolIndexEntry) -> DiscoveredTool:
    tool_name = fqn.split(".", 1)[1]
    return DiscoveredTool(
        fqn=fqn,
        name=tool_name,
        plugin=entry.plugin,
        description=entry.description,
        parameters=entry.parameters,
    )


async def _ensure_tool_index() -> None:
    """Build or refresh the mounted jarvis.* tool index from capability definitions."""
    from core.plugins.registry import registry

    global _tool_index_signature, _tool_index

    signature = _tool_signature()
    if signature == _tool_index_signature and _tool_index:
        return

    index: dict[str, _ToolIndexEntry] = {}
    for definition in registry.iter_capabilities(enabled_only=True):
        fqn = definition.fqn
        if fqn == "system.search_tools":
            continue
        doc = definition.documentation or ""
        first_line = (doc.split("\n", 1)[0]).strip() if doc else ""
        sig_str = definition.visible_signature_str
        param_names = _visible_param_names_from_sig(definition.visible_signature)
        required_param_names = _visible_required_param_names_from_sig(
            definition.visible_signature
        )
        param_docs = _extract_param_docs(doc, param_names)
        param_text = " ".join(param_docs.values())
        text = " ".join(
            part for part in (
                f"{fqn}{sig_str}",
                first_line,
                param_text,
            ) if part
        )
        index[fqn] = _ToolIndexEntry(
            plugin=definition.plugin,
            description=first_line,
            signature=sig_str,
            required_parameter_names=required_param_names,
            parameters=param_docs,
            search_text=text,
        )

    _tool_index = index
    _tool_index_signature = signature


async def _ensure_tool_vectors() -> None:
    """Embed the tool index only when lexical matching is insufficient."""
    from services.embeddings import embedding_service

    missing = [fqn for fqn, entry in _tool_index.items() if entry.vector is None]
    if not missing:
        return

    vectors = await asyncio.to_thread(
        embedding_service.embed,
        [_tool_index[fqn].search_text for fqn in missing],
    )
    for fqn, vector in zip(missing, vectors):
        entry = _tool_index[fqn]
        _tool_index[fqn] = _ToolIndexEntry(
            plugin=entry.plugin,
            description=entry.description,
            signature=entry.signature,
            required_parameter_names=entry.required_parameter_names,
            parameters=entry.parameters,
            search_text=entry.search_text,
            vector=vector,
        )


async def _run_shell(
    command: str,
    *,
    timeout: int = EXEC_TIMEOUT_SECS,
    cwd: str | Path | None = None,
) -> ExecResult:
    """Run a shell command with timeout and process cleanup."""
    workdir = None
    if cwd is not None:
        workdir = str(Path(cwd).expanduser().resolve())

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise

    result = _prepare_exec_streams(
        stdout_b.decode(errors="replace").strip(),
        stderr_b.decode(errors="replace").strip(),
    )
    result.exit_code = proc.returncode or 0
    return result


async def _exec(command: str, timeout: int = EXEC_TIMEOUT_SECS) -> tuple[str, int]:
    """Compat helper for internal diagnostics: combined output + returncode."""
    result = await _run_shell(command, timeout=timeout)
    parts = [p for p in (result.stdout, result.stderr) if p]
    return "\n".join(parts), result.exit_code


# --- Diagnostics helpers ---

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


async def _diag_battery(detailed: bool = False) -> str:
    batt = psutil.sensors_battery()
    if batt is None:
        return "No battery (desktop or battery info unavailable)."
    pct = round(batt.percent)
    if batt.power_plugged:
        if batt.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
            summary = f"Battery at {pct}%, plugged in and charged."
        else:
            summary = f"Battery at {pct}%, plugged in. Full in about {_fmt_duration(batt.secsleft)}."
    elif batt.secsleft == psutil.POWER_TIME_UNKNOWN:
        summary = f"Battery at {pct}%, on battery (time remaining unknown)."
    else:
        summary = f"Battery at {pct}%, on battery. About {_fmt_duration(batt.secsleft)} remaining."

    if not detailed:
        return summary

    if platform.system() == "Darwin":
        out, rc = await _exec("system_profiler SPPowerDataType 2>/dev/null", timeout=10)
        if rc == 0:
            extras = []
            for line in out.splitlines():
                stripped = line.strip()
                for key in ("Cycle Count", "Condition", "Maximum Capacity"):
                    if stripped.startswith(key):
                        extras.append(stripped)
            if extras:
                summary += "\n  " + "\n  ".join(extras)

    return summary


async def _diag_cpu(detailed: bool = False) -> str:
    cpu_pct = await asyncio.to_thread(psutil.cpu_percent, 0.5)
    cores_logical = psutil.cpu_count(logical=True)
    cores_physical = psutil.cpu_count(logical=False)
    load1, *_ = psutil.getloadavg()
    load_pct = round(load1 / cores_logical * 100) if cores_logical else 0
    if load_pct < 30:
        load_desc = "light"
    elif load_pct < 70:
        load_desc = "moderate"
    else:
        load_desc = "heavy"
    if cores_physical and cores_physical != cores_logical:
        core_str = f"{cores_physical} cores ({cores_logical} logical)"
    else:
        core_str = f"{cores_logical or '?'} cores"
    summary = (
        f"CPU at {round(cpu_pct)}% across {core_str}. "
        f"Load is {load_desc} ({load_pct}% over last minute)."
    )

    if not detailed:
        return summary

    lines = [summary]
    per_core = await asyncio.to_thread(psutil.cpu_percent, 0.5, True)
    core_strs = [f"{round(p)}%" for p in per_core]
    lines.append(f"  Per-core: {', '.join(core_strs)}")
    try:
        freq = psutil.cpu_freq()
        if freq and freq.current:
            freq_str = f"  Frequency: {round(freq.current)} MHz"
            if freq.min and freq.max:
                freq_str += f" (range: {round(freq.min)}–{round(freq.max)} MHz)"
            lines.append(freq_str)
    except (AttributeError, NotImplementedError):
        pass
    stats = psutil.cpu_stats()
    lines.append(f"  Context switches: {stats.ctx_switches:,} | Interrupts: {stats.interrupts:,}")
    return "\n".join(lines)


def _diag_memory(detailed: bool = False) -> str:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    avail = _fmt_bytes(mem.available)
    total = _fmt_bytes(mem.total)
    pct = round(mem.percent)
    if pct < 50:
        pressure = "low"
    elif pct < 80:
        pressure = "moderate"
    else:
        pressure = "high"
    summary = f"Memory: {avail} free of {total} ({pct}% used, {pressure} pressure)."
    if swap.total > 0:
        summary += f" Swap: {round(swap.percent)}% used."

    if not detailed:
        return summary

    lines = [summary]
    parts = []
    for attr in ("active", "inactive", "wired", "buffers", "cached", "shared"):
        val = getattr(mem, attr, None)
        if val is not None:
            parts.append(f"{attr.capitalize()}: {_fmt_bytes(val)}")
    if parts:
        lines.append(f"  {' | '.join(parts)}")
    if swap.total > 0:
        lines.append(
            f"  Swap: {_fmt_bytes(swap.used)} of {_fmt_bytes(swap.total)} "
            f"({round(swap.percent)}%)"
        )
    return "\n".join(lines)


def _diag_disk(detailed: bool = False) -> str:
    usage = psutil.disk_usage("/")
    free = _fmt_bytes(usage.free)
    total = _fmt_bytes(usage.total)
    pct = round(usage.percent)
    summary = f"Disk: {free} free of {total} ({pct}% used)."

    if not detailed:
        return summary

    lines = [summary]
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint == "/":
            continue
        try:
            pu = psutil.disk_usage(part.mountpoint)
            if pu.total < 100 * 1024 * 1024:
                continue
            lines.append(
                f"  {part.mountpoint}: {_fmt_bytes(pu.free)} free of "
                f"{_fmt_bytes(pu.total)} ({round(pu.percent)}%, {part.fstype})"
            )
        except (PermissionError, OSError):
            continue
    try:
        io = psutil.disk_io_counters()
        if io:
            lines.append(
                f"  I/O since boot: {io.read_count:,} reads ({_fmt_bytes(io.read_bytes)}) | "
                f"{io.write_count:,} writes ({_fmt_bytes(io.write_bytes)})"
            )
    except Exception:
        pass
    return "\n".join(lines)


async def _diag_network(detailed: bool = False) -> str:
    lines = []

    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    active_iface_ips: dict[str, str] = {}
    for iface, addr_list in addrs.items():
        if iface.startswith("lo"):
            continue
        if stats.get(iface) and not stats[iface].isup:
            continue
        for addr in addr_list:
            if addr.family.name == "AF_INET" and not addr.address.startswith("127."):
                active_iface_ips[iface] = addr.address

    if active_iface_ips:
        lines.append("IPs: " + ", ".join(f"{k}: {v}" for k, v in active_iface_ips.items()))
    else:
        lines.append("No active network interfaces.")

    if platform.system() == "Darwin":
        wifi_iface = next(
            (iface for iface in active_iface_ips if iface.startswith("en")),
            "en0",
        )
        ssid_out, rc = await _exec(
            f"networksetup -listpreferredwirelessnetworks {wifi_iface} 2>/dev/null",
            timeout=5,
        )
        if rc == 0:
            entries = [l.strip() for l in ssid_out.splitlines() if l.strip() and "Preferred" not in l]
            if entries:
                lines.append(f"WiFi: {entries[0]}")
    elif platform.system() == "Linux":
        ssid_out, rc = await _exec(
            "nmcli -t -f active,ssid dev wifi 2>/dev/null | grep '^yes'",
            timeout=5,
        )
        if rc == 0 and ssid_out:
            lines.append(f"WiFi: {ssid_out.split(':', 1)[-1].strip()}")

    if not detailed:
        return "\n".join(lines)

    io = psutil.net_io_counters()
    if io:
        lines.append(
            f"  Traffic since boot: ↑ {_fmt_bytes(io.bytes_sent)} sent | "
            f"↓ {_fmt_bytes(io.bytes_recv)} received"
        )
        lines.append(
            f"  Packets: ↑ {io.packets_sent:,} | ↓ {io.packets_recv:,} | "
            f"Errors: {io.errin + io.errout:,} | Drops: {io.dropin + io.dropout:,}"
        )
    try:
        conns = psutil.net_connections(kind="inet")
        established = sum(1 for c in conns if c.status == "ESTABLISHED")
        listening = sum(1 for c in conns if c.status == "LISTEN")
        lines.append(f"  Connections: {established} established, {listening} listening")
    except (psutil.AccessDenied, OSError):
        pass

    return "\n".join(lines)


def _diag_uptime() -> str:
    secs = time.time() - psutil.boot_time()
    return f"System uptime: {_fmt_duration(secs)}."


async def _diag_dict(detailed: bool = False) -> Dict[str, str]:
    """Return diagnostics as a named-key dict the LLM can index directly."""
    battery = await _diag_battery(detailed)
    cpu = await _diag_cpu(detailed)
    memory = _diag_memory(detailed)
    disk = _diag_disk(detailed)
    network = await _diag_network(detailed)
    uptime = _diag_uptime()

    # Compact one-liner for speaking aloud
    parts = [cpu.split(".")[0], memory.split(".")[0], disk.split(".")[0]]
    if "No battery" not in battery:
        parts.insert(0, battery.split(".")[0])
    summary = ". ".join(parts) + "."

    return {
        "summary": summary,
        "battery": battery,
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
        "network": network,
        "uptime": uptime,
    }



class SystemPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="system",
        version="2.0.0",
        description="Core system controls: volume, app launching, shell exec with allow/ask/deny.",
    )

    # --- Pre-built tools ---

    @tool
    async def search_tools(self, query: str) -> ToolSearchResult:
        """Search mounted jarvis.* tools by name/docs, with semantic fallback.

        Use when a needed capability is not among the tools offered this turn.
        Results include the exact FQN and parameter hints. The runtime offers
        returned tools on the next iteration — call the newly offered tool then.
        If no mounted result fits and the user needs an external service, try
        composio.search_catalog next.
        """
        from services.embeddings import embedding_service

        try:
            await _ensure_tool_index()
        except Exception as e:
            logger.error("system.search_tools index failed: %s", e)
            return ToolSearchResult(tools=[])

        query_tokens = _search_tokens(query)
        lexical: list[tuple[int, DiscoveredTool]] = []
        for fqn, entry in _tool_index.items():
            entry_tokens = _search_tokens(entry.search_text)
            overlap = len(query_tokens & entry_tokens)
            if query.lower() in entry.search_text.lower():
                overlap += 3
            if overlap:
                lexical.append((overlap, _tool_card(fqn, entry)))

        lexical.sort(key=lambda item: item[0], reverse=True)
        if lexical and lexical[0][0] >= 2:
            return ToolSearchResult(tools=[result for _, result in lexical[:10]])

        try:
            await _ensure_tool_vectors()
            query_vector = await asyncio.to_thread(embedding_service.embed_one, query)
        except Exception as e:
            logger.error("system.search_tools embedding failed: %s", e)
            return ToolSearchResult(tools=[])

        scored: list[tuple[float, DiscoveredTool]] = []
        for fqn, entry in _tool_index.items():
            if entry.vector is None:
                continue
            score = embedding_service.cosine_similarity(query_vector, entry.vector)
            scored.append((score, _tool_card(fqn, entry)))

        scored.sort(key=lambda item: item[0], reverse=True)
        return ToolSearchResult(tools=[result for _, result in scored[:10]])

    @tool
    async def think(self, thought: str) -> str:
        """
        Free scratchpad to organise your approach before acting. Zero cost, not shown to the user.

        Use on any non-trivial turn when the right course of action is not immediately
        obvious, the task has multiple steps, or you need to weigh options before committing.
        Writing a plan here reduces incorrect tool calls.

        Do NOT use for simple lookups or single-step actions — the overhead is not worth it.
        MUST be followed by a tool call that acts on the plan; never let think() be the last
        action.
        """
        return "[Thought recorded]"

    @tool
    async def stop_listening(self) -> str:
        """
        Stop listening and return to passive (wake-word) mode.
        Call this ONCE when the user says goodbye or indicates they are finished.
        After calling, say a brief farewell. Do NOT call this again.
        """
        owner_id = get_owner_id()
        connection_id = get_connection_id()
        await event_bus.publish(
            Event(
                type=EventType.VOICE_SESSION_END,
                source="system_plugin",
                data={
                    "owner_id": owner_id,
                    "session_id": connection_id,
                    "connection_id": connection_id,
                },
            )
        )
        return "Session ended. Respond to the user with a brief farewell."

    @tool
    async def set_volume(self, level: int | None = None, delta: int | None = None) -> str | CapabilityErrorDetail:
        """
        Set or adjust system volume. Use level= for absolute (0–100) or delta= for relative.
        "Volume to 50" → level=50. "Turn it down a bit" → delta=-10. "Mute" → level=0.
        """
        if delta is not None:
            current_out, rc = await _exec("osascript -e 'output volume of (get volume settings)'")
            if rc != 0:
                return _fail(f"Failed to read current volume (exit {rc}).")
            try:
                current = int(current_out.strip())
            except ValueError:
                return _fail(f"Could not parse current volume: {current_out!r}")
            level = current + delta

        if level is None:
            return _fail("Provide either level= or delta=.")

        level = max(0, min(100, level))
        _, rc = await _exec(f"osascript -e 'set volume output volume {level}'")
        if rc == 0:
            return f"Volume set to {level}."
        return _fail(f"Failed to set volume (exit {rc}).")

    @tool
    async def open_application(self, name: str) -> str | CapabilityErrorDetail:
        """
        Launch a macOS application by name.
        "Open Spotify", "Launch Terminal", "Open Safari" → this tool.
        Confirm briefly after it opens.
        """
        output, rc = await _exec(f'open -a "{name}"')
        if rc == 0:
            return f"Opened {name}."
        return _fail(f"Could not open '{name}': {output or 'app not found'}.")

    @tool
    async def quit_application(self, name: str) -> str | CapabilityErrorDetail:
        """
        Quit a macOS application by name.
        "Quit Spotify", "Close Safari", "Exit Terminal" → this tool.
        Confirm briefly after it quits.
        """
        output, rc = await _exec(f"""osascript -e 'quit app "{name}"'""")
        if rc == 0:
            return f"Quit {name}."
        return _fail(f"Could not quit '{name}': {output or 'app not running'}.")

    @tool
    async def get_status(self) -> str:
        """
        Return a summary of JARV1S system status including connected integrations.
        "How are you doing?", "System status", "Are you running?" → this tool.
        For full reminder/alarm details, use scheduler.get_alerts(); status only gives a compact notification summary.
        VOICE: Speak naturally — don't list raw numbers, describe them.
        """
        from core.config import settings
        from core.scheduling import local_datetime_fields
        from services.database.mongodb import mongodb

        uptime_secs = int(time.time() - _start_time)
        hours, rem = divmod(uptime_secs, 3600)
        minutes = rem // 60

        owner_id = get_owner_id()
        notification_count = await mongodb.db.trigger_instances.count_documents(
            {"owner_id": owner_id, "status": {"$in": ["pending", "awaiting_delivery"]}}
        )
        notification_docs = await mongodb.db.trigger_instances.find(
            {"owner_id": owner_id, "status": {"$in": ["pending", "awaiting_delivery"]}},
            {
                "due_at": 1,
                "action_snapshot.message": 1,
                "attention_snapshot.sound": 1,
                "origin_snapshot.timezone": 1,
            },
        ).sort("due_at", 1).limit(3).to_list(length=3)
        profile_data = await mongodb.get_tool_data(owner_id, "profile")
        fact_count = len(profile_data.get("facts", []))

        uptime_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"
        notification_summary = f"{notification_count} pending notification(s)"
        if notification_docs:
            labels = []
            for doc in notification_docs:
                message = (doc.get("action_snapshot") or {}).get("message") or "Notification"
                sound = (doc.get("attention_snapshot") or {}).get("sound") or "chime"
                timezone_name = (doc.get("origin_snapshot") or {}).get("timezone")
                fields = local_datetime_fields(doc.get("due_at"), timezone_name=timezone_name)
                labels.append(f"{message} ({sound}, {fields['local_time']})")
            suffix = "" if notification_count <= len(labels) else f", plus {notification_count - len(labels)} more"
            notification_summary = f"{notification_count} pending notification(s): {', '.join(labels)}{suffix}"
        from core.setup.llm_config import resolve_llm_config_sync

        llm_model = resolve_llm_config_sync().model or "not configured"
        parts = [
            f"Running for {uptime_str}. "
            f"Version {settings.VERSION}, model {llm_model}. "
            f"{notification_summary}. {fact_count} stored fact(s)."
        ]

        try:
            from core.integrations.lifecycle import list_integrations
            views = await list_integrations()
            connected = [v for v in views if v.connected and v.status != "error"]
            needs_setup = [v for v in views if v.status == "error"]
            if connected:
                names = ", ".join(v.display_name for v in connected)
                parts.append(f"Connected integrations ({len(connected)}): {names}.")
            elif views:
                parts.append("No integrations connected.")
            if needs_setup:
                names = ", ".join(v.display_name for v in needs_setup)
                parts.append(f"Needs auth setup ({len(needs_setup)}): {names}.")
        except Exception:
            pass

        return " ".join(parts)

    @tool
    async def list_integrations(self) -> str:
        """
        List all available integrations and their connection status.
        "What integrations do I have?", "Is GitHub connected?", "Which tools are active?" → this tool.
        VOICE: Summarize naturally — "GitHub is connected with 12 tools", not raw data.
        """
        from core.integrations.lifecycle import list_integrations as list_integrations_lifecycle

        views = await list_integrations_lifecycle()
        if not views:
            return "No integrations are configured."

        lines = []
        for v in views:
            if v.status == "error":
                status = "needs setup"
            elif v.connected:
                status = "connected"
            else:
                status = "not connected"
            tool_info = f", {v.tool_count} tools loaded" if v.connected and v.status != "error" and v.tool_count else ""
            error_info = f" ({v.last_error})" if v.last_error else ""
            lines.append(f"- {v.display_name}: {status}{tool_info}{error_info}")

        return "\n".join(lines)

    @tool
    async def diagnostics(self, category: str = "overview", detailed: bool = False) -> Dict[str, str]:
        """
        Report machine health. Returns a dict with named keys: summary, battery, cpu, memory,
        disk, network, uptime. Each value is a human-readable string.
        The summary key is a compact one-liner — speak it aloud, push the rest to screen.
        category: overview | battery | cpu | memory | disk | network.
        detailed=True for per-core CPU, per-partition disk, network I/O, battery cycle count.
        "How's my battery?", "Give me detailed diagnostics", "Is my Mac slow?" → this tool.
        """
        cat = category.lower().strip()
        if cat == "overview":
            return await _diag_dict(detailed)
        dispatch = {
            "battery": _diag_battery,
            "cpu": _diag_cpu,
            "network": _diag_network,
        }
        if cat in dispatch:
            text = await dispatch[cat](detailed)
        elif cat == "memory":
            text = _diag_memory(detailed)
        else:
            text = _diag_disk(detailed)
        return {"summary": text, cat: text}


    @tool
    async def top_processes(self, sort_by: str = "cpu", limit: int = 5) -> str:
        """
        List the top processes by resource usage. sort_by: 'cpu' or 'memory'. limit: 1–10.
        "What's eating my CPU?", "Why is my Mac slow?", "What's using the most memory?" → this tool.
        VOICE: translate raw process names to the app the user would recognise and summarize
        conversationally. Never read technical names or numbers verbatim.
        Example: "Cursor and Chrome are your biggest resource hogs right now. Everything else is quiet."
        """
        sort_by = sort_by.lower().strip()
        limit = max(1, min(10, limit))
        attr = "memory_percent" if sort_by == "memory" else "cpu_percent"

        def _collect():
            for p in psutil.process_iter():
                try:
                    p.cpu_percent()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            time.sleep(0.5)
            procs = []
            for p in psutil.process_iter():
                try:
                    info = {
                        "name": p.name(),
                        "cpu_percent": p.cpu_percent(),
                        "memory_percent": p.memory_percent(),
                    }
                    if info["name"] and info[attr] is not None:
                        procs.append(info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return sorted(procs, key=lambda x: x[attr] or 0, reverse=True)[:limit]

        procs = await asyncio.to_thread(_collect)
        if not procs:
            return "No process data available."

        is_cpu = attr == "cpu_percent"
        label = "memory" if sort_by == "memory" else "CPU"
        lines = [f"Top {limit} by {label}:"]
        for p in procs:
            raw = p[attr] or 0
            pct = round(raw)
            if is_cpu and pct > 100:
                core_count = raw / 100
                lines.append(f"  {p['name']}: ~{core_count:.1f} cores ({pct}%)")
            else:
                lines.append(f"  {p['name']}: {pct}%")
        return "\n".join(lines)

    @tool
    async def repeat_last(self) -> str | CapabilityErrorDetail:
        """
        Retrieve the last spoken response.
        "What did you say?", "Can you repeat that?", "Say that again" → this tool.
        Read the returned text back VERBATIM.
        """
        from services.database.mongodb import mongodb

        owner_id = get_owner_id()
        last = await mongodb.get_last_spoken_response(owner_id)
        if not last:
            return _fail("Nothing to repeat yet.")
        return last

    # --- Shell execution ---

    @tool
    async def exec(
        self,
        command: str,
        cwd: str = "~",
        timeout_seconds: int = EXEC_TIMEOUT_SECS,
    ) -> str | CapabilityErrorDetail:
        """Run a shell command on the user's Mac.

        Prefer jarvis.files.* for file work. Use this for diagnostics and OS actions
        no dedicated tool covers. Commands run by default. File removal, git push,
        and disk erasure require approval; secrets paths, destructive patterns, and
        piping downloads into a shell are denied.
        VOICE: summarize the outcome in one line; never read the raw command.
        """
        timeout = max(1, min(int(timeout_seconds), 120))
        verdict, reason = classify_exec(command)

        if verdict == ExecVerdict.DENY:
            return _fail(reason)

        async def _do_exec() -> str | CapabilityErrorDetail:
            try:
                result = await _run_shell(command, timeout=timeout, cwd=cwd)
            except asyncio.TimeoutError:
                return _fail(f"Command timed out after {timeout}s and was terminated.")
            return _format_exec_result(result)

        if verdict == ExecVerdict.ALLOW:
            return await _do_exec()

        description = f"Run shell command: {command[:120]}"
        return await require_consent(description, _do_exec, detail=command)

    @tool
    async def approve_pending(self) -> str:
        """
        Execute the pending action after the user has confirmed.
        ONLY call this after the user explicitly says yes to the pending action.
        Never call this speculatively.
        The returned result is the source of truth for what actually executed.
        """
        owner_id = get_owner_id()
        return await execute_pending(owner_id)

    @tool
    async def deny_pending(self) -> str:
        """
        Cancel the pending action and resolve the pending input.
        Call when the user says no, or to clean up a stale approval.
        """
        owner_id = get_owner_id()
        return await cancel_pending(owner_id)

    @tool
    async def resolve_pending_input(self, input_id: str, decision: str) -> str | CapabilityErrorDetail:
        """
        Resolve a specific pending approval from PendingInputWidget.
        decision must be "approve" or "deny". Use approve_pending/deny_pending
        for voice yes/no follow-ups when the input id is not available.
        """
        from core.pending_inputs import resolve_pending_input

        normalized = decision.strip().lower()
        if normalized not in {"approve", "deny"}:
            return _fail('Invalid decision. Use "approve" or "deny".')
        return await resolve_pending_input(
            owner_id=get_owner_id(),
            input_id=input_id,
            decision=normalized,  # type: ignore[arg-type]
        )

    @tool
    async def refresh_integrations(self) -> str | CapabilityErrorDetail:
        """
        Reload non-Composio MCP server tool schemas from their live servers.
        Clears the on-disk cache, deregisters stale plugins, and reconnects.
        Use for bespoke/HTTP/stdio MCP bridges whose schemas seem outdated.
        """
        from core.integrations.lifecycle import refresh_non_composio_integrations

        refreshed_names = await refresh_non_composio_integrations()
        count = len(refreshed_names)
        if count == 0:
            return _fail("No non-Composio integrations were refreshed.")
        return f"Refreshed {count} non-Composio integration{'s' if count != 1 else ''}."

    @tool
    async def connect_integration(self, name: str) -> ToolResult | CapabilityErrorDetail:
        """
        Start the connection flow for any integration (OAuth or Composio).
        For Google/Microsoft-backed integrations (calendar, gmail), pushes an OAuth setup widget.
        For Composio integrations (spotify, github, slack, notion), sends a Connect Link widget.
        "Connect Calendar", "Set up Google auth", "Link my Spotify" → this tool.
        name: the integration name (e.g. 'calendar', 'gmail', 'spotify') or provider ('google').
        VOICE: Say "I've sent you a setup widget — follow the steps on screen."
        """
        from core.integrations.manager import integrations as integrations_mgr

        name = name.strip().lower().replace(" ", "_")

        provider = integrations_mgr.resolve_oauth_provider(name)

        if provider:
            return await self._connect_oauth_provider(provider)

        from core.integrations.lifecycle import (
            IntegrationLifecycleError,
            create_connect_link,
        )

        try:
            connect_url = await create_connect_link(name)
        except IntegrationLifecycleError as e:
            return _fail(str(e))

        envelope = UIEnvelope(
            widget_id=generate_id(f"connect-{name}-"),
            component="ConnectWidget",
            data={
                "integration_name": name,
                "app_name": name.title(),
                "connect_url": connect_url,
                "status": "pending",
            },
            layout=WidgetLayout(size=WidgetSize.WIDE, priority=15),
            title=f"Connect {name.title()}",
        )
        return ToolResult(
            content=f"Connect link sent for {name}. Ask the user to tap the widget to authorize.",
            ui=[envelope],
        )

    async def _connect_oauth_provider(self, provider: str) -> ToolResult:
        """Build the OAuthWidget ToolResult for a bespoke OAuth provider (google, microsoft)."""
        from core.auth.manager import auth_manager
        from core.auth.providers import is_connectable

        account_email = None
        try:
            token = await auth_manager.get_token(provider)
            account_email = token.account_email
        except Exception:
            pass

        if account_email:
            return ToolResult(content=f"{provider.title()} is already connected as {account_email}.")

        connectable = await is_connectable(provider)
        envelope = UIEnvelope(
            widget_id=generate_id(f"oauth-{provider}-"),
            component="OAuthWidget",
            data={
                "provider": provider,
                "account_email": account_email,
            },
            layout=WidgetLayout(size=WidgetSize.WIDE, priority=15),
            title=f"{provider.title()} OAuth",
        )

        if not connectable:
            content = (
                f"{provider.title()} OAuth is not configured for this instance. "
                f"I've opened the auth widget — use the advanced setup option to add your OAuth app."
            )
        else:
            content = (
                f"I've opened the {provider.title()} auth widget — tap Authorize to sign in."
            )
        return ToolResult(content=content, ui=[envelope])

    @tool
    async def disconnect_integration(self, name: str) -> str:
        """
        Disconnect an integration and remove its credentials/tools.
        "Disconnect Google", "Remove Spotify", "Reset my Microsoft account" → this tool.
        name: the integration or provider name (e.g. 'google', 'microsoft', 'spotify').
        """
        from core.integrations.manager import integrations
        normalized = name.strip().lower()

        if normalized in integrations.get_bespoke_providers():
            from core.auth.manager import auth_manager
            await auth_manager.delete_provider(normalized)
            return f"Disconnected {name}. You'll need to re-authorize next time you use a {name} integration."

        from core.integrations.lifecycle import (
            IntegrationLifecycleError,
            disconnect_integration as disconnect_integration_lifecycle,
        )

        try:
            result = await disconnect_integration_lifecycle(name)
        except IntegrationLifecycleError as e:
            return str(e)

        if result.remote_disconnected:
            return f"Disconnected {name}. Its tools are no longer available."
        return f"{name.title()} was already disconnected remotely and has been removed locally."

    @tool
    async def reset_integration(self, name: str) -> str:
        """
        Force-reset a broken Composio integration by purging its stale MCP server and auth config,
        then reconnecting from scratch. Use when an integration shows as connected but tools fail
        with auth errors like "Auth config could not be resolved".
        name: the Composio toolkit slug (e.g. 'spotify', 'github', 'slack').
        "Fix Spotify auth", "Reset Spotify connection", "Spotify tools aren't working" → this tool.
        """
        from core.integrations.composio_gateway import get_composio_gateway
        from core.integrations.lifecycle import (
            IntegrationLifecycleError,
            disconnect_integration as disconnect_integration_lifecycle,
        )
        from core.integrations.mcp.cache import invalidate_cache

        normalized = name.strip().lower()
        gateway = get_composio_gateway()
        if not gateway:
            return _fail("Composio is not configured on this instance.")

        # 1. Disconnect the existing connected account (if any)
        try:
            await disconnect_integration_lifecycle(normalized)
        except IntegrationLifecycleError:
            pass  # Already disconnected or bespoke guard — carry on

        # 2. Purge stale MCP server + auth config from Composio and memory
        await gateway.reset_mcp_server(normalized)

        # 3. Invalidate the local schema cache so the next reconnect fetches fresh tools
        invalidate_cache(normalized)

        return (
            f"{name.title()} auth has been fully reset. "
            f"Please reconnect via the Tools panel or ask me to connect {name.title()} again."
        )

    @tool
    async def retrain_wakeword(self) -> str | CapabilityErrorDetail:
        """
        Retrain the wake word model using collected feedback samples, then hot-swap the active model.
        "Retrain the wake word", "Update wake word model", "Improve wake word detection" → this tool.
        Reports how many samples were used and whether training succeeded. Takes 1-5 minutes.
        """
        from core.config import settings

        training_dir = settings.BASE_DIR.parent / "training" / "wakeword"
        venv_python = training_dir / "venv" / "bin" / "python"
        python_bin = str(venv_python) if venv_python.exists() else "python3"
        timeout = 600  # 10 minutes max

        # Count samples before training
        feedback_base = training_dir / "data" / "feedback"
        pos_count = len(list((feedback_base / "positives").glob("*.wav"))) if (feedback_base / "positives").exists() else 0
        neg_count = len(list((feedback_base / "negatives").glob("*.wav"))) if (feedback_base / "negatives").exists() else 0

        if pos_count == 0:
            return _fail("No feedback samples collected yet. Use the wake word a few times first, then retrain.")

        td = shlex.quote(str(training_dir))
        py = shlex.quote(python_bin)

        # Step 1: Train
        try:
            train_out, train_rc = await _exec(f"cd {td} && {py} scripts/train.py", timeout=timeout)
        except asyncio.TimeoutError:
            return _fail(f"Training timed out after {timeout}s and was terminated.")
        if train_rc != 0:
            return _fail(f"Training failed (exit {train_rc}):\n{train_out}")

        # Step 2: Export ONNX
        try:
            export_out, export_rc = await _exec(f"cd {td} && {py} scripts/export_onnx.py", timeout=120)
        except asyncio.TimeoutError:
            return _fail("ONNX export timed out after 120s.")
        if export_rc != 0:
            return _fail(f"ONNX export failed (exit {export_rc}):\n{export_out}")

        # Step 3: Copy new model to runtime location
        exported = training_dir / "models" / "jarvis_au_adapter.onnx"
        if not exported.exists():
            return _fail("Training completed but exported ONNX not found. Check export_onnx.py output path.")

        dest = settings.BASE_DIR / "resources" / "models" / "wakeword" / "Jarvis.onnx"
        shutil.copy2(exported, dest)

        eval_summary = ""
        try:
            eval_out, eval_rc = await _exec(
                f"cd {settings.BASE_DIR} && {py} tools/eval_wakeword.py "
                f"--model ../training/wakeword/models/jarvis_au_adapter.onnx",
                timeout=180,
            )
            tail = "\n".join(eval_out.strip().splitlines()[-3:])
            eval_summary = f"\n\nPost-export eval (exit {eval_rc}):\n{tail}"
        except asyncio.TimeoutError:
            eval_summary = "\n\nPost-export eval timed out after 180s."

        # Step 4: Hot-swap the running model
        try:
            from api.websockets.connection import manager
            session = manager.get_session(settings.DEFAULT_USER_ID)
            if session and session.processor.wakeword_service:
                session.processor.wakeword_service.reload()
                hot_swap = "Model hot-swapped — active immediately."
            else:
                hot_swap = "Model saved but no active session to hot-swap. Restart to apply."
        except Exception as e:
            hot_swap = f"Model saved. Hot-swap failed ({e}). Restart to apply."

        return (
            f"Retrain complete. Used {pos_count} positive and {neg_count} negative feedback samples. "
            f"{hot_swap}{eval_summary}"
        )
