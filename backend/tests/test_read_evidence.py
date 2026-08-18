"""Contract tests for shared lookup evidence vocabulary."""

from __future__ import annotations

from core.plugins.capabilities import InvocationStatus, status_for_result
from core.plugins.read_evidence import (
    MatchStatus,
    ReadCoverage,
    ReadEvidence,
)
from plugins.calendar.models import CalendarQueryResult
from plugins.gmail import EmailQueryResult
from plugins.scheduler import AlertQueryResult


def test_domain_query_results_conform_to_read_evidence() -> None:
    calendar = CalendarQueryResult(
        events=[],
        time_min="2026-05-01T00:00:00+00:00",
        time_max="2026-05-02T00:00:00+00:00",
        match_status=MatchStatus.NONE,
        coverage=ReadCoverage.COMPLETE,
    )
    email = EmailQueryResult(
        emails=[],
        query="in:inbox",
        match_status=MatchStatus.NONE,
        coverage=ReadCoverage.COMPLETE,
    )
    alert = AlertQueryResult(
        alerts=[],
        match_status=MatchStatus.NONE,
    )

    assert isinstance(calendar, ReadEvidence)
    assert isinstance(email, ReadEvidence)
    assert isinstance(alert, ReadEvidence)
    assert alert.coverage is ReadCoverage.COMPLETE


def test_partial_domain_result_is_successful_invocation() -> None:
    result = CalendarQueryResult(
        events=[],
        time_min="2026-05-01T00:00:00+00:00",
        time_max="2026-05-02T00:00:00+00:00",
        match_status=MatchStatus.NONE,
        coverage=ReadCoverage.PARTIAL,
        failed_providers=["microsoft"],
    )
    assert status_for_result(result) is InvocationStatus.SUCCEEDED
