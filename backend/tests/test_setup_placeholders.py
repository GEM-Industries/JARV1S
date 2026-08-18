import pytest

from core.setup.placeholders import is_placeholder_api_key


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),
        ("", True),
        ("   ", True),
        ("your_deepinfra_key", True),
        ("your_openrouter_key", True),
        ("sk-ant-...", True),
        ("changeme", True),
        ("placeholder", True),
        ("sk-real-key-abcdef1234567890", False),
        ("hf_abcdefghijklmnop", False),
    ],
)
def test_is_placeholder_api_key(value, expected):
    assert is_placeholder_api_key(value) is expected
