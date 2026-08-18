"""Tests for shared trigger instance projection helpers."""

from __future__ import annotations

from core.triggers.projection import trigger_run_kind, trigger_run_source


def test_kind_classifies_external_with_rule_id_as_automation() -> None:
    doc = {
        "origin_snapshot": {"kind": "external", "source": "calendar"},
        "source_event": {"rule_id": "rule-1"},
    }
    assert trigger_run_kind(doc) == "automation"


def test_kind_treats_external_without_rule_id_as_trigger() -> None:
    doc = {
        "origin_snapshot": {"kind": "external", "source": "calendar"},
        "source_event": {},
    }
    assert trigger_run_kind(doc) == "trigger"


def test_kind_treats_time_origin_as_trigger() -> None:
    doc = {
        "origin_snapshot": {"kind": "time"},
        "source_event": {"rule_id": "rule-1"},
    }
    assert trigger_run_kind(doc) == "trigger"


def test_kind_handles_missing_snapshots() -> None:
    assert trigger_run_kind({}) == "trigger"
    assert trigger_run_kind({"origin_snapshot": None, "source_event": None}) == "trigger"


def test_source_prefers_rule_name() -> None:
    doc = {
        "source_event": {
            "rule_name": "Morning check",
            "protocol_name": "Startup",
            "trigger_source": "system_pulse",
        },
        "action_snapshot": {"protocol_name": "Other"},
        "origin_snapshot": {"kind": "external", "source": "calendar"},
    }
    assert trigger_run_source(doc) == "Morning check"


def test_source_falls_back_through_chain() -> None:
    assert trigger_run_source(
        {"source_event": {}, "action_snapshot": {"protocol_name": "Startup"}, "origin_snapshot": {}}
    ) == "Startup"
    assert trigger_run_source(
        {"source_event": {}, "action_snapshot": {}, "origin_snapshot": {"source": "gmail"}}
    ) == "gmail"
    assert trigger_run_source(
        {"source_event": {}, "action_snapshot": {}, "origin_snapshot": {"kind": "time"}}
    ) == "time"


def test_source_returns_none_when_no_label_available() -> None:
    assert trigger_run_source({}) is None
    assert trigger_run_source({"source_event": {}, "action_snapshot": {}, "origin_snapshot": {}}) is None
