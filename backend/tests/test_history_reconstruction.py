import json

from core.turns.history import reconstruct_adapter_history


def test_reconstruct_adapter_history_pairs_structured_calls():
    messages = reconstruct_adapter_history([
        {"role": "user", "content": "turn on the kitchen light", "metadata": {}},
        {
            "role": "assistant",
            "content": "On it.\n\nsmart_home.turn_on({\"entity_id\":\"light.kitchen\"})",
            "metadata": {
                "turn_type": "tool_call",
                "tool_call_id": "tc-1",
                "capability": "smart_home.turn_on",
                "provider_name": "smart_home__turn_on",
                "arguments": {"entity_id": "light.kitchen"},
                "spoken": "On it.",
            },
        },
        {
            "role": "user",
            "content": "ok",
            "metadata": {
                "turn_type": "tool_result",
                "tool_call_id": "tc-1",
                "output": "Kitchen light is on.",
            },
        },
        {
            "role": "assistant",
            "content": "Done.",
            "metadata": {"turn_type": "text_only"},
        },
    ])

    assert messages[0] == {"role": "user", "content": "turn on the kitchen light"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "On it."
    assert messages[1]["tool_calls"][0]["function"]["name"] == "smart_home__turn_on"
    assert json.loads(messages[1]["tool_calls"][0]["function"]["arguments"]) == {"entity_id": "light.kitchen"}
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "tc-1",
        "content": "Kitchen light is on.",
    }
    assert messages[3] == {"role": "assistant", "content": "Done."}


def test_reconstruct_adapter_history_drops_incomplete_pairs():
    messages = reconstruct_adapter_history([
        {
            "role": "assistant",
            "content": "Checking.",
            "metadata": {
                "turn_type": "tool_call",
                "tool_call_id": "tc-missing",
                "capability": "calendar.get_events",
                "arguments": {},
                "spoken": "Checking.",
            },
        },
        {"role": "user", "content": "never mind", "metadata": {}},
    ])

    assert messages == [
        {"role": "assistant", "content": "Checking."},
        {"role": "user", "content": "never mind"},
    ]
