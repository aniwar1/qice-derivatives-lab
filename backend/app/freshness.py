from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import ValidationError

from .models import LatestStrategyRecord


# A valid record is never removed merely because its suggested review time has
# elapsed.  ``stale`` is an advisory state that tells the UI to wait for the
# user to click the refresh action; it is intentionally different from an
# unavailable or malformed record.
FreshnessState = Literal["current", "stale", "missing", "invalid"]
FreshnessReason = Literal[
    "no_completed_strategy",
    "reassess_by_elapsed",
    "session_partial_daily_close_elapsed",
    "stored_record_invalid",
]


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


@dataclass(frozen=True, slots=True)
class StrategyFreshness:
    """Public freshness state for the most recent completed database record.

    A valid record remains publishable after its suggested review deadline.
    ``stale`` is an advisory marker for manual refresh, not an automatic
    deletion or hiding rule.  Missing and malformed records still fail closed.
    """

    state: FreshnessState
    reason: FreshnessReason | None
    checked_at: datetime
    strategy_updated_at: datetime | None = None
    strategy_as_of: datetime | None = None
    reassess_by: datetime | None = None
    valid_until: datetime | None = None
    valid_until_reason: FreshnessReason | None = None

    @property
    def is_current(self) -> bool:
        return self.state == "current"

    @property
    def requires_manual_refresh(self) -> bool:
        return self.state == "stale"

    def model_dump(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "is_current": self.is_current,
            "requires_manual_refresh": self.requires_manual_refresh,
            "checked_at": _iso(self.checked_at),
            "strategy_updated_at": _iso(self.strategy_updated_at),
            "strategy_as_of": _iso(self.strategy_as_of),
            "reassess_by": _iso(self.reassess_by),
            "valid_until": _iso(self.valid_until),
            "valid_until_reason": self.valid_until_reason,
        }


@dataclass(frozen=True, slots=True)
class LatestStrategyResolution:
    record: LatestStrategyRecord | None
    freshness: StrategyFreshness


def resolve_latest_strategy(
    payload: dict[str, Any] | LatestStrategyRecord | None,
    *,
    now: datetime | None = None,
) -> LatestStrategyResolution:
    """Resolve a stored record into a manual-refresh-aware public view.

    The effective review marker is the earlier of ``reassess_by`` and, for a
    partial session bar, its expected daily close.  A completed bar is not
    invalidated by its historical ``expected_daily_close_at`` value.  Once the
    marker is reached, the validated record is still returned so the user can
    inspect it and decide when to request a new snapshot.
    """

    checked_at = _aware(now or datetime.now(tz=timezone.utc), "now")
    if payload is None:
        return LatestStrategyResolution(
            record=None,
            freshness=StrategyFreshness(
                state="missing",
                reason="no_completed_strategy",
                checked_at=checked_at,
            ),
        )

    try:
        record = (
            payload
            if isinstance(payload, LatestStrategyRecord)
            else LatestStrategyRecord.model_validate(payload)
        )
        _aware(record.updated_at, "updated_at")
        _aware(record.strategy.as_of, "strategy.as_of")
        _aware(record.strategy.reassess_by, "strategy.reassess_by")
    except (ValidationError, TypeError, ValueError):
        return LatestStrategyResolution(
            record=None,
            freshness=StrategyFreshness(
                state="invalid",
                reason="stored_record_invalid",
                checked_at=checked_at,
            ),
        )

    deadlines: list[tuple[datetime, FreshnessReason]] = [
        (record.strategy.reassess_by, "reassess_by_elapsed")
    ]
    session_bar = record.strategy.session_daily_bar
    if session_bar is not None and session_bar.bar_state == "session_partial":
        deadlines.append(
            (
                session_bar.coverage.expected_daily_close_at,
                "session_partial_daily_close_elapsed",
            )
        )

    valid_until, expiry_reason = min(deadlines, key=lambda item: item[0])
    stale = checked_at >= valid_until
    return LatestStrategyResolution(
        record=record,
        freshness=StrategyFreshness(
            state="stale" if stale else "current",
            reason=expiry_reason if stale else None,
            checked_at=checked_at,
            strategy_updated_at=record.updated_at,
            strategy_as_of=record.strategy.as_of,
            reassess_by=record.strategy.reassess_by,
            valid_until=valid_until,
            valid_until_reason=expiry_reason,
        ),
    )
