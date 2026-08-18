"""Operations read models."""

from core.operations.definitions import (
    AutomationDefinitionSummary,
    SetupExplain,
    SetupSummary,
    explain_setup,
    list_automation_definitions,
    list_setups,
)
from core.operations.models import OperationRunDetail
from core.operations.service import get_trigger_run_detail, get_user_turn_detail, list_user_turns

__all__ = [
    "OperationRunDetail",
    "AutomationDefinitionSummary",
    "SetupExplain",
    "SetupSummary",
    "explain_setup",
    "get_trigger_run_detail",
    "get_user_turn_detail",
    "list_automation_definitions",
    "list_setups",
    "list_user_turns",
]
