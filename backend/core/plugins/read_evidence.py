"""Shared lookup evidence vocabulary for domain-owned query results.

Two orthogonal axes:

- ``MatchStatus`` — how many candidates the tool returned
- ``ReadCoverage`` — whether absence claims are authoritative

Domain models keep their item lists and failure details. Truncation and
provider/fetch gaps are reasons for ``PARTIAL``; they are not a third
coverage value. Invocation ledger status stays separate: a successful
partial read is still ``InvocationStatus.SUCCEEDED``.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable


class MatchStatus(str, Enum):
    NONE = "none"
    SINGLE = "single"
    MULTIPLE = "multiple"


class ReadCoverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@runtime_checkable
class ReadEvidence(Protocol):
    match_status: MatchStatus
    coverage: ReadCoverage


def match_status_from_count(count: int) -> MatchStatus:
    if count <= 0:
        return MatchStatus.NONE
    if count == 1:
        return MatchStatus.SINGLE
    return MatchStatus.MULTIPLE
