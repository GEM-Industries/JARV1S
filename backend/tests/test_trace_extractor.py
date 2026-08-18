"""Tests for evals/trace_extractor.py."""

from __future__ import annotations

from core.turns.delivery import TurnResult
from evals.trace_extractor import extract_turn_trace


def _tool_call_trace(capability: str, arguments: dict, *, spoken: str = "", output: str = "Success: ok") -> list:
    preview = f"{capability}({arguments})"
    return [
        ("user", "delete readme"),
        (
            "assistant",
            f"{spoken}\n\n{preview}" if spoken else preview,
            {
                "turn_type": "tool_call",
                "capability": capability,
                "arguments": arguments,
                "spoken": spoken,
                "tool_call_id": "tc-1",
                "model": "test-model",
                "routed_tools": ["files.delete"],
            },
        ),
        (
            "user",
            output,
            {
                "turn_type": "tool_result",
                "tool_call_id": "tc-1",
                "output": output,
                "capability": capability,
                "focus_tools": ["files.delete"],
                "invocations": [
                    {
                        "invocation_id": "inv-1",
                        "capability": "files.delete",
                        "status": "blocked",
                        "args_preview": {"path": "~/project/README.md"},
                    }
                ],
            },
        ),
        (
            "assistant",
            "Need approval first.",
            {
                "turn_type": "text_only",
                "model": "test-model",
                "tools_called": ["files.delete"],
                "routed_tools": ["files.delete"],
            },
        ),
    ]


def test_extract_turn_trace_tool_call_and_output() -> None:
    result = TurnResult(
        model="test-model",
        routed_tools=["files.delete"],
        tools_called=["files.delete"],
        full_response="Need approval first.",
        turn_trace=_tool_call_trace(
            "files.delete",
            {"path": "~/project/README.md"},
            spoken="I'll request approval.",
            output="APPROVAL_NEEDED: delete README.md",
        ),
    )

    snapshot = extract_turn_trace(result)

    assert snapshot.model == "test-model"
    assert snapshot.routed_tools == ["files.delete"]
    assert snapshot.tools_called == ["files.delete"]
    assert snapshot.full_response == "Need approval first."
    assert len(snapshot.tool_calls) == 1
    assert snapshot.tool_calls[0].capability == "files.delete"
    assert snapshot.tool_calls[0].arguments["path"] == "~/project/README.md"
    assert snapshot.tool_calls[0].fqns == ("files.delete",)
    assert snapshot.tool_calls[0].invocations[0]["status"] == "blocked"
    assert snapshot.tool_outputs == ["APPROVAL_NEEDED: delete README.md"]


def test_extract_turn_trace_no_reply_text_only() -> None:
    result = TurnResult(
        model="test-model",
        full_response="NO_REPLY",
        turn_trace=[
            ("system", "SYSTEM EVENT: chatter"),
            ("assistant", "NO_REPLY", {"turn_type": "text_only", "model": "test-model"}),
        ],
    )

    snapshot = extract_turn_trace(result)
    assert snapshot.full_response == "NO_REPLY"
    assert snapshot.tool_calls == []
