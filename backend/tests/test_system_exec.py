import pytest

from core.plugins.capabilities import CapabilityErrorDetail
from plugins import system as system_mod
from plugins.files import FilesPlugin
from plugins.system import (
    SystemPlugin,
    _format_exec_result,
    _prepare_exec_streams,
)


@pytest.mark.asyncio
async def test_exec_allow_runs_echo():
    out = await SystemPlugin().exec("echo jarvis-exec-ok")
    assert isinstance(out, str)
    assert "jarvis-exec-ok" in out
    assert "[exit" not in out  # exit 0 has no footer


@pytest.mark.asyncio
async def test_exec_deny_sudo():
    raw = await SystemPlugin().exec("sudo ls")
    assert isinstance(raw, CapabilityErrorDetail)
    assert raw.code == "tool_error"
    assert "sudo" in raw.message


@pytest.mark.asyncio
async def test_exec_ask_path_uses_consent(monkeypatch):
    from plugins.system_exec_policy import ExecVerdict

    async def fake_consent(description, action, detail=""):
        return CapabilityErrorDetail(
            code="approval_needed",
            message=f"Approval needed: {description} The action has not executed yet.",
        )

    monkeypatch.setattr(
        "plugins.system.classify_exec",
        lambda _cmd: (ExecVerdict.ASK, "ask"),
    )
    monkeypatch.setattr("plugins.system.require_consent", fake_consent)
    raw = await SystemPlugin().exec("open -a Safari")
    assert isinstance(raw, CapabilityErrorDetail)
    assert raw.code == "approval_needed"


def test_prepare_exec_streams_spills_when_over_line_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(system_mod, "TOOL_OUTPUT_DIR", tmp_path / "tool-output")
    monkeypatch.setattr(system_mod, "_display_spill_path", lambda path: str(path))

    huge = "\n".join(f"line-{i}" for i in range(200))
    result = _prepare_exec_streams(huge, "")
    assert result.truncated
    assert result.spill_path is not None
    assert "line-0" in result.stdout
    assert "line-199" in result.stdout
    assert "line-100" not in result.stdout  # middle dropped from preview

    spill_files = list((tmp_path / "tool-output").glob("exec_*.log"))
    assert len(spill_files) == 1
    assert huge in spill_files[0].read_text()
    assert spill_files[0].stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "tool-output").stat().st_mode & 0o777 == 0o700

    formatted = _format_exec_result(result)
    assert "Full output saved to:" in formatted
    assert "jarvis.files.grep" in formatted
    assert "jarvis.files.read" in formatted


@pytest.mark.asyncio
async def test_spilled_output_is_readable_by_files_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(system_mod, "TOOL_OUTPUT_DIR", tmp_path / "tool-output")
    monkeypatch.setattr(system_mod, "_display_spill_path", lambda path: str(path))
    monkeypatch.setattr("plugins.files.Path.home", lambda: tmp_path)
    monkeypatch.setattr("plugins.files.BLOCKED_PATHS", ())

    result = _prepare_exec_streams("\n".join(f"line-{i}" for i in range(300)), "")

    output = await FilesPlugin().read(result.spill_path)
    assert "line-0" in output
    assert "offset=" in output


def test_prepare_exec_streams_clips_single_huge_line_by_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(system_mod, "TOOL_OUTPUT_DIR", tmp_path / "tool-output")
    monkeypatch.setattr(system_mod, "_display_spill_path", lambda path: str(path))

    huge = "x" * (system_mod.MAX_OUTPUT_BYTES * 2)
    result = _prepare_exec_streams(huge, "")

    assert result.truncated
    assert result.spill_path is not None
    assert "bytes truncated" in result.stdout
    assert len(result.stdout.encode()) < system_mod.MAX_OUTPUT_BYTES + 200
    assert list((tmp_path / "tool-output").glob("exec_*.log"))[0].read_text() == huge


def test_prepare_exec_streams_no_spill_when_small():
    result = _prepare_exec_streams("hello\nworld", "")
    assert not result.truncated
    assert result.spill_path is None
    assert _format_exec_result(result) == "hello\nworld"
