import pytest

from core.setup.readiness import SetupNotReadyError, require_llm_ready
from core.setup.runtime import jarvis_runtime
from tests.test_setup_helpers import _unconfigured_config


def test_headless_turn_guard_skips_when_not_ready(monkeypatch):
    """Headless path should fail closed before model work when setup is incomplete."""
    jarvis_runtime.core_ready = False
    monkeypatch.setattr(
        "core.setup.readiness.resolve_llm_config_sync",
        lambda: _unconfigured_config(),
    )
    with pytest.raises(SetupNotReadyError):
        require_llm_ready()
