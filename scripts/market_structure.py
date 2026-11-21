#!/usr/bin/env python3
"""Causal market-structure levels derived from completed daily bars only.

The module deliberately separates structural observations from trade decisions.
Every pivot records when it occurred and when its right-hand confirmation made it
observable.  Calling the public functions with ``as_of`` therefore recreates the
information set that existed at that date instead of silently using later bars.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
import hashlib
import json
from math import ceil, exp, floor, isfinite, log
from pathlib import Path
import re
from statistics import median
import sys
from typing import Any, Iterable, Literal, Mapping, Sequence

try:
    from scripts.runtime_universe import sina_provider_symbol_for_contract
    from scripts.tick_evidence import TickEvidenceError, replay_exact_tick_evidence
except ModuleNotFoundError:
    from runtime_universe import sina_provider_symbol_for_contract
    from tick_evidence import TickEvidenceError, replay_exact_tick_evidence


PivotKind = Literal["high", "low"]
LevelKind = Literal["support", "resistance"]
TrendDirection = Literal["up", "down", "range"]
BreakoutDirection = Literal["up", "down"]
TradeDirection = Literal["long", "short"]
BreakoutState = Literal[
    "none",
    "probing",
    "confirmed_breakout",
    "false_breakout",
    "retest_held",
    "retest_failed",
    "conflict",
]
STRUCTURE_SCHEMA_VERSION = "cn_futures_market_structure.v1"
STRUCTURE_ALGORITHM_VERSION = "causal_pivots_zones_theil_sen_tick_aligned.v1"
PROJECT = Path(__file__).resolve().parents[1]
PRODUCT_SPECS_PATH = PROJECT / "config" / "product_contract_specs.json"


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily bar whose completion status is explicit."""

    at: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    open_interest: float | None = None
    completed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _as_date(self.at))
        prices = (self.open, self.high, self.low, self.close)
        if not all(isfinite(value) for value in prices):
            raise ValueError("OHLC prices must be finite")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must not be below another OHLC price")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must not be above another OHLC price")
        for name in ("volume", "open_interest"):
            value = getattr(self, name)
            if value is not None and (not isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class Pivot:
    """A swing point that becomes usable only after right-side confirmation."""

    kind: PivotKind
    price: float
    pivot_at: date
    confirmed_at: date
    available_at: date
    pivot_index: int
    confirmed_index: int


@dataclass(frozen=True, slots=True)
class StructureLevel:
    """A price zone assembled from causally available swing points."""

    kind: LevelKind
    lower: float
    center: float
    upper: float
    strength: float
    pivot_count: int
    touches: int
    rejections: int
    crosses: int
    pivot_at: tuple[date, ...]
    confirmed_at: tuple[date, ...]
    available_at: date
    last_interaction_at: date | None

    def __post_init__(self) -> None:
        if not self.lower <= self.center <= self.upper:
            raise ValueError("level prices must satisfy lower <= center <= upper")
        if self.pivot_count != len(self.pivot_at):
            raise ValueError("pivot_count must match pivot_at")


@dataclass(frozen=True, slots=True)
class TrendLine:
    """A robustly projected line from confirmed, same-side pivots."""

    direction: TrendDirection
    side: LevelKind | None
    value: float | None
    raw_projection: float | None
    slope_per_bar: float | None
    intercept: float | None
    projected_at: date
    anchors: tuple[Pivot, ...]
    available_at: date | None
    fit_median_absolute_error: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class BreakoutAssessment:
    """Causal status of a possible break and retest around one price zone."""

    state: BreakoutState
    direction: BreakoutDirection
    at: date | None
    available_at: date | None
    fail_closed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class StructureSnapshot:
    """The compact structural state available at one completed-bar cutoff."""

    as_of: date
    reference_price: float
    support: StructureLevel | None
    resistance: StructureLevel | None
    trend: TrendLine
    pivots: tuple[Pivot, ...]
    completed_bar_count: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation for scanners and decision ledgers."""

        def convert(value: Any) -> Any:
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, tuple):
                return [convert(item) for item in value]
            if isinstance(value, list):
                return [convert(item) for item in value]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        return convert(asdict(self))


# Backward-friendly descriptive alias.  New callers should use StructureSnapshot.
MarketStructure = StructureSnapshot


@dataclass(frozen=True, slots=True)
class TradeGeometry:
    """Entry, invalidation, and target-space ordering for one direction."""

    direction: TradeDirection
    entry: float
    invalidation: float
    target_boundary: float
    valid: bool
    reason: str


def completed_bars_as_of(
    bars: Iterable[Bar], as_of: date | datetime | str | None = None
) -> tuple[Bar, ...]:
    """Return sorted, unique, completed bars no later than ``as_of``."""

    cutoff = _as_date(as_of) if as_of is not None else None
    selected = sorted(
        (
            bar
            for bar in bars
            if bar.completed and (cutoff is None or bar.at <= cutoff)
        ),
        key=lambda bar: bar.at,
    )
    if not selected:
        raise ValueError("no completed daily bars are available at the cutoff")
    if len({bar.at for bar in selected}) != len(selected):
        raise ValueError("completed daily bars must have unique dates")
    return tuple(selected)


def find_confirmed_pivots(
    bars: Iterable[Bar],
    *,
    as_of: date | datetime | str | None = None,
    left_window: int = 2,
    right_window: int = 2,
) -> tuple[Pivot, ...]:
    """Find unique swing highs/lows with an explicit right confirmation window."""

    if left_window < 1 or right_window < 1:
        raise ValueError("pivot windows must both be positive")
    completed = completed_bars_as_of(bars, as_of)
    pivots: list[Pivot] = []
    window_size = left_window + right_window + 1
    if len(completed) < window_size:
        return ()

    for index in range(left_window, len(completed) - right_window):
        window = completed[index - left_window : index + right_window + 1]
        bar = completed[index]
        highs = [item.high for item in window]
        lows = [item.low for item in window]
        is_high = bar.high == max(highs) and highs.count(bar.high) == 1
        is_low = bar.low == min(lows) and lows.count(bar.low) == 1

        # An outside bar can look like both a swing high and low, but daily OHLC
        # cannot reveal which extreme happened first.  Skipping it is fail-closed.
        if is_high and is_low:
            continue

        confirmed_index = index + right_window
        confirmed_at = completed[confirmed_index].at
        if is_high:
            pivots.append(
                Pivot(
                    kind="high",
                    price=bar.high,
                    pivot_at=bar.at,
                    confirmed_at=confirmed_at,
                    available_at=confirmed_at,
                    pivot_index=index,
                    confirmed_index=confirmed_index,
                )
            )
        elif is_low:
            pivots.append(
                Pivot(
                    kind="low",
                    price=bar.low,
                    pivot_at=bar.at,
                    confirmed_at=confirmed_at,
                    available_at=confirmed_at,
                    pivot_index=index,
                    confirmed_index=confirmed_index,
                )
            )
    return tuple(pivots)


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a quantile of an empty sequence")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower_index = floor(position)
    upper_index = ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = position - lower_index
    return ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight


def _round_down(value: float, tick_size: float) -> float:
    return _round_to_tick(value, tick_size, ROUND_FLOOR)


def _round_up(value: float, tick_size: float) -> float:
    return _round_to_tick(value, tick_size, ROUND_CEILING)


def _round_to_tick(value: float, tick_size: float, rounding: str) -> float:
    """Round by decimal tick units without leaking binary-float artifacts."""

    decimal_value = Decimal(str(value))
    decimal_tick = Decimal(str(tick_size))
    if not decimal_value.is_finite() or not decimal_tick.is_finite() or decimal_tick <= 0:
        raise ValueError("value and tick_size must be finite; tick_size must be positive")
    units = decimal_value / decimal_tick
    nearest = units.to_integral_value(rounding=ROUND_HALF_EVEN)
    # Calculations upstream use floats, so collapse only sub-nanotick noise
    # before applying the directional floor/ceil rule.
    if abs(units - nearest) <= Decimal("1e-9"):
        rounded_units = nearest
    else:
        rounded_units = units.to_integral_value(rounding=rounding)
    return float(rounded_units * decimal_tick)


def derive_zone_half_width(
    bars: Iterable[Bar],
    *,
    center: float,
    tick_size: float,
    as_of: date | datetime | str | None = None,
    rolling_window: int = 60,
) -> float:
    """Derive zone tolerance from ticks, close changes, and observed crossings."""

    if not isfinite(tick_size) or tick_size <= 0:
        raise ValueError("tick_size must be a positive finite number")
    if rolling_window < 5:
        raise ValueError("rolling_window must be at least 5")
    completed = completed_bars_as_of(bars, as_of)[-rolling_window:]
    changes = [
        abs(current.close - previous.close)
        for previous, current in zip(completed, completed[1:])
    ]
    typical_change = median(changes) if changes else tick_size

    crossing_depths: list[float] = []
    for bar in completed:
        if bar.low <= center <= bar.high:
            # Both sides were printed; the smaller side is the depth that is
            # certain to have crossed the center regardless of intrabar order.
            crossing_depths.append(min(bar.high - center, center - bar.low))
    typical_crossing = median(crossing_depths) if crossing_depths else 0.0
    change_cap = _quantile(changes, 0.75) if changes else tick_size
    empirical_width = max(
        typical_change * 0.35,
        min(typical_crossing * 0.5, change_cap),
    )
    return _round_up(max(tick_size, empirical_width), tick_size)


def _cluster_pivots(
    pivots: Sequence[Pivot],
    completed: Sequence[Bar],
    tick_size: float,
) -> list[list[Pivot]]:
    clusters: list[list[Pivot]] = []
    for pivot in sorted(pivots, key=lambda item: item.price):
        pivot_width = derive_zone_half_width(
            completed,
            center=pivot.price,
            tick_size=tick_size,
            as_of=completed[-1].at,
        )
        matching_index: int | None = None
        matching_distance = float("inf")
        for index, cluster in enumerate(clusters):
            cluster_center = median(item.price for item in cluster)
            cluster_width = derive_zone_half_width(
                completed,
                center=cluster_center,
                tick_size=tick_size,
                as_of=completed[-1].at,
            )
            distance = abs(pivot.price - cluster_center)
            if distance <= max(pivot_width, cluster_width) and distance < matching_distance:
                matching_index = index
                matching_distance = distance
        if matching_index is None:
            clusters.append([pivot])
        else:
            clusters[matching_index].append(pivot)
    return clusters


def _decay(age: int, half_life: float) -> float:
    return exp(-log(2) * max(age, 0) / half_life)


def _level_from_cluster(
    cluster: Sequence[Pivot],
    *,
    kind: LevelKind,
    completed: Sequence[Bar],
    tick_size: float,
) -> StructureLevel:
    center = median(pivot.price for pivot in cluster)
    empirical_half_width = derive_zone_half_width(
        completed,
        center=center,
        tick_size=tick_size,
        as_of=completed[-1].at,
    )
    half_width = _round_up(max(tick_size, empirical_half_width), tick_size)
    lower = _round_down(center - half_width, tick_size)
    upper = _round_up(center + half_width, tick_size)

    touches = 0
    rejections = 0
    crosses = 0
    event_score = 0.0
    last_interaction_index: int | None = None
    previous_touched = False
    previous_close: float | None = None
    final_index = len(completed) - 1

    for index, bar in enumerate(completed):
        touched = bar.high >= lower and bar.low <= upper
        if touched:
            last_interaction_index = index
        if touched and not previous_touched:
            touches += 1
            event_score += 0.8 * _decay(final_index - index, 90)
        rejected = (
            touched
            and (
                (kind == "support" and bar.close > upper and bar.close >= bar.open)
                or (
                    kind == "resistance"
                    and bar.close < lower
                    and bar.close <= bar.open
                )
            )
        )
        if rejected:
            rejections += 1
            event_score += 1.4 * _decay(final_index - index, 90)
        crossed = previous_close is not None and (
            (kind == "support" and previous_close >= lower and bar.close < lower)
            or (
                kind == "resistance"
                and previous_close <= upper
                and bar.close > upper
            )
        )
        if crossed:
            crosses += 1
            event_score -= 1.8 * _decay(final_index - index, 120)
        previous_touched = touched
        previous_close = bar.close

    pivot_score = sum(
        2.0 * _decay(final_index - pivot.confirmed_index, 120)
        for pivot in cluster
    )
    strength = round(max(0.0, pivot_score + event_score), 6)
    last_interaction_at = (
        completed[last_interaction_index].at
        if last_interaction_index is not None
        else None
    )
    return StructureLevel(
        kind=kind,
        lower=lower,
        center=center,
        upper=upper,
        strength=strength,
        pivot_count=len(cluster),
        touches=touches,
        rejections=rejections,
        crosses=crosses,
        pivot_at=tuple(pivot.pivot_at for pivot in cluster),
        confirmed_at=tuple(pivot.confirmed_at for pivot in cluster),
        available_at=max(pivot.available_at for pivot in cluster),
        last_interaction_at=last_interaction_at,
    )


def build_structure_levels(
    bars: Iterable[Bar],
    *,
    tick_size: float,
    as_of: date | datetime | str | None = None,
    left_window: int = 2,
    right_window: int = 2,
) -> tuple[StructureLevel, ...]:
    """Build support and resistance zones from confirmed pivots."""

    completed = completed_bars_as_of(bars, as_of)
    pivots = find_confirmed_pivots(
        completed,
        as_of=completed[-1].at,
        left_window=left_window,
        right_window=right_window,
    )
    levels: list[StructureLevel] = []
    for pivot_kind, level_kind in (("low", "support"), ("high", "resistance")):
        matching = [pivot for pivot in pivots if pivot.kind == pivot_kind]
        for cluster in _cluster_pivots(matching, completed, tick_size):
            levels.append(
                _level_from_cluster(
                    cluster,
                    kind=level_kind,
                    completed=completed,
                    tick_size=tick_size,
                )
            )
    return tuple(levels)


def select_strongest_levels(
    levels: Iterable[StructureLevel], reference_price: float
) -> tuple[StructureLevel | None, StructureLevel | None]:
    """Select exactly one strongest zone below and above the reference price."""

    if not isfinite(reference_price):
        raise ValueError("reference_price must be finite")
    support_candidates = [
        level
        for level in levels
        if level.kind == "support" and level.upper < reference_price
    ]
    resistance_candidates = [
        level
        for level in levels
        if level.kind == "resistance" and level.lower > reference_price
    ]

    def support_key(level: StructureLevel) -> tuple[float, int, int, float]:
        return (
            level.strength,
            level.rejections,
            level.pivot_count,
            level.upper,
        )

    def resistance_key(level: StructureLevel) -> tuple[float, int, int, float]:
        return (
            level.strength,
            level.rejections,
            level.pivot_count,
            -level.lower,
        )

    support = max(support_candidates, key=support_key, default=None)
    resistance = max(resistance_candidates, key=resistance_key, default=None)
    return support, resistance


def _theil_sen(anchors: Sequence[Pivot]) -> tuple[float, float, float]:
    slopes = [
        (right.price - left.price) / (right.pivot_index - left.pivot_index)
        for left_index, left in enumerate(anchors)
        for right in anchors[left_index + 1 :]
        if right.pivot_index != left.pivot_index
    ]
    if not slopes:
        raise ValueError("trend projection needs pivots at distinct bar indexes")
    slope = median(slopes)
    intercept = median(
        anchor.price - slope * anchor.pivot_index for anchor in anchors
    )
    residuals = [
        abs(anchor.price - (intercept + slope * anchor.pivot_index))
        for anchor in anchors
    ]
    return slope, intercept, median(residuals)


def project_trend_line(
    bars: Iterable[Bar],
    pivots: Iterable[Pivot],
    *,
    tick_size: float,
    as_of: date | datetime | str | None = None,
    max_anchors: int = 6,
) -> TrendLine:
    """Project an up/down/range line from at least three same-side pivots."""

    if max_anchors < 3:
        raise ValueError("max_anchors must be at least 3")
    if not isfinite(tick_size) or tick_size <= 0:
        raise ValueError("tick_size must be a positive finite number")
    completed = completed_bars_as_of(bars, as_of)
    cutoff = completed[-1].at
    available = [pivot for pivot in pivots if pivot.available_at <= cutoff]
    lows = sorted(
        (pivot for pivot in available if pivot.kind == "low"),
        key=lambda pivot: pivot.pivot_index,
    )[-max_anchors:]
    highs = sorted(
        (pivot for pivot in available if pivot.kind == "high"),
        key=lambda pivot: pivot.pivot_index,
    )[-max_anchors:]

    recent = completed[-61:]
    changes = [
        abs(current.close - previous.close)
        for previous, current in zip(recent, recent[1:])
    ]
    typical_change = median(changes) if changes else tick_size
    meaningful_slope = max(tick_size * 0.1, typical_change * 0.05)

    low_fit = _theil_sen(lows) if len(lows) >= 3 else None
    high_fit = _theil_sen(highs) if len(highs) >= 3 else None
    rising_support = low_fit is not None and low_fit[0] > meaningful_slope
    falling_resistance = high_fit is not None and high_fit[0] < -meaningful_slope

    if rising_support and not falling_resistance:
        direction: TrendDirection = "up"
        side: LevelKind | None = "support"
        anchors = tuple(lows)
        fit = low_fit
        reason = "rising_support_from_confirmed_swing_lows"
    elif falling_resistance and not rising_support:
        direction = "down"
        side = "resistance"
        anchors = tuple(highs)
        fit = high_fit
        reason = "falling_resistance_from_confirmed_swing_highs"
    elif rising_support and falling_resistance:
        # A contracting structure gives incompatible directional boundaries.
        direction = "range"
        side = None
        anchors = ()
        fit = None
        reason = "opposing_confirmed_boundaries_fail_closed"
    else:
        direction = "range"
        candidates: list[
            tuple[float, LevelKind, list[Pivot], tuple[float, float, float]]
        ] = []
        if low_fit is not None:
            candidates.append((low_fit[2], "support", lows, low_fit))
        if high_fit is not None:
            candidates.append((high_fit[2], "resistance", highs, high_fit))
        if candidates:
            _, side, selected_anchors, fit = min(candidates, key=lambda item: item[0])
            anchors = tuple(selected_anchors)
            reason = "confirmed_boundary_is_flat_or_directionally_inconclusive"
        else:
            side = None
            anchors = ()
            fit = None
            reason = "insufficient_confirmed_same_side_pivots"

    if fit is None:
        return TrendLine(
            direction=direction,
            side=side,
            value=None,
            raw_projection=None,
            slope_per_bar=None,
            intercept=None,
            projected_at=cutoff,
            anchors=anchors,
            available_at=None,
            fit_median_absolute_error=None,
            reason=reason,
        )

    slope, intercept, fit_error = fit
    raw_projection = intercept + slope * (len(completed) - 1)
    # A support line is actionable only at or below its raw projection; a
    # resistance line only at or above it.  Directional rounding avoids a
    # nearest-tick choice that can cross the fitted boundary.
    value = (
        _round_down(raw_projection, tick_size)
        if side == "support"
        else _round_up(raw_projection, tick_size)
    )
    return TrendLine(
        direction=direction,
        side=side,
        value=value,
        raw_projection=raw_projection,
        slope_per_bar=slope,
        intercept=intercept,
        projected_at=cutoff,
        anchors=anchors,
        available_at=max(anchor.available_at for anchor in anchors),
        fit_median_absolute_error=fit_error,
        reason=reason,
    )


def assess_breakout(
    bars: Iterable[Bar],
    level: StructureLevel,
    *,
    direction: BreakoutDirection,
    as_of: date | datetime | str | None = None,
    confirmation_closes: int = 2,
) -> BreakoutAssessment:
    """Classify probing, false/confirmed breaks, and post-break retests.

    A bar that trades through both sides of the zone while also testing the
    breakout side has unknowable intrabar ordering in daily OHLC.  The result is
    therefore ``conflict`` and must not be treated as a valid trigger.
    """

    if confirmation_closes < 2:
        raise ValueError("confirmation_closes must be at least 2")
    completed = completed_bars_as_of(bars, as_of)
    usable = [bar for bar in completed if bar.at >= level.available_at]
    if not usable:
        return BreakoutAssessment(
            state="none",
            direction=direction,
            at=None,
            available_at=None,
            fail_closed=False,
            reason="no_completed_bar_since_level_became_available",
        )

    breakout_boundary = level.upper if direction == "up" else level.lower
    opposite_boundary = level.lower if direction == "up" else level.upper
    consecutive_closes = 0
    first_break_at: date | None = None
    confirmed_at: date | None = None
    latest_probe_at: date | None = None

    for bar in usable:
        broke_intraday = (
            bar.high > breakout_boundary
            if direction == "up"
            else bar.low < breakout_boundary
        )
        violated_opposite = (
            bar.low < opposite_boundary
            if direction == "up"
            else bar.high > opposite_boundary
        )
        if broke_intraday and violated_opposite:
            return BreakoutAssessment(
                state="conflict",
                direction=direction,
                at=bar.at,
                available_at=bar.at,
                fail_closed=True,
                reason="same_bar_OHLC_crosses_both_zone_boundaries_intrabar_order_unknown",
            )

        closed_beyond = (
            bar.close > breakout_boundary
            if direction == "up"
            else bar.close < breakout_boundary
        )
        if confirmed_at is None:
            if closed_beyond:
                if consecutive_closes == 0:
                    first_break_at = bar.at
                consecutive_closes += 1
                if consecutive_closes >= confirmation_closes:
                    confirmed_at = bar.at
                continue

            if consecutive_closes > 0:
                return BreakoutAssessment(
                    state="false_breakout",
                    direction=direction,
                    at=bar.at,
                    available_at=bar.at,
                    fail_closed=False,
                    reason="close_returned_inside_before_confirmation",
                )
            if broke_intraday:
                latest_probe_at = bar.at
                if (
                    (direction == "up" and bar.close <= level.upper)
                    or (direction == "down" and bar.close >= level.lower)
                ):
                    return BreakoutAssessment(
                        state="false_breakout",
                        direction=direction,
                        at=bar.at,
                        available_at=bar.at,
                        fail_closed=False,
                        reason="intraday_break_rejected_by_completed_close",
                    )
            continue

        touched_zone = bar.low <= level.upper and bar.high >= level.lower
        failed_retest = (
            bar.close < opposite_boundary
            if direction == "up"
            else bar.close > opposite_boundary
        )
        if failed_retest:
            return BreakoutAssessment(
                state="retest_failed",
                direction=direction,
                at=bar.at,
                available_at=bar.at,
                fail_closed=False,
                reason="completed_close_violated_opposite_zone_boundary_after_break",
            )
        if touched_zone:
            held = (
                bar.close > level.upper
                if direction == "up"
                else bar.close < level.lower
            )
            if held:
                return BreakoutAssessment(
                    state="retest_held",
                    direction=direction,
                    at=bar.at,
                    available_at=bar.at,
                    fail_closed=False,
                    reason="completed_retest_held_beyond_breakout_boundary",
                )

    if confirmed_at is not None:
        return BreakoutAssessment(
            state="confirmed_breakout",
            direction=direction,
            at=confirmed_at,
            available_at=confirmed_at,
            fail_closed=False,
            reason=f"{confirmation_closes}_consecutive_completed_closes_beyond_zone",
        )
    if latest_probe_at is not None or first_break_at is not None:
        at = latest_probe_at or first_break_at
        return BreakoutAssessment(
            state="probing",
            direction=direction,
            at=at,
            available_at=at,
            fail_closed=False,
            reason="breakout_not_yet_confirmed_by_completed_closes",
        )
    return BreakoutAssessment(
        state="none",
        direction=direction,
        at=None,
        available_at=None,
        fail_closed=False,
        reason="no_breakout_evidence",
    )


def analyze_market_structure(
    bars: Iterable[Bar],
    *,
    tick_size: float,
    as_of: date | datetime | str | None = None,
    reference_price: float | None = None,
    left_window: int = 2,
    right_window: int = 2,
) -> StructureSnapshot:
    """Return one strongest support, resistance, and robust trend state."""

    completed = completed_bars_as_of(bars, as_of)
    cutoff = completed[-1].at
    pivots = find_confirmed_pivots(
        completed,
        as_of=cutoff,
        left_window=left_window,
        right_window=right_window,
    )
    levels = build_structure_levels(
        completed,
        tick_size=tick_size,
        as_of=cutoff,
        left_window=left_window,
        right_window=right_window,
    )
    price = completed[-1].close if reference_price is None else reference_price
    support, resistance = select_strongest_levels(levels, price)
    trend = project_trend_line(
        completed,
        pivots,
        tick_size=tick_size,
        as_of=cutoff,
    )
    return StructureSnapshot(
        as_of=cutoff,
        reference_price=price,
        support=support,
        resistance=resistance,
        trend=trend,
        pivots=pivots,
        completed_bar_count=len(completed),
    )


def _bar_from_mapping(value: Mapping[str, Any]) -> Bar:
    try:
        at = value.get("at", value.get("date"))
        return Bar(
            at=at,
            open=float(value["open"]),
            high=float(value["high"]),
            low=float(value["low"]),
            close=float(value["close"]),
            volume=(
                None if value.get("volume") is None else float(value["volume"])
            ),
            open_interest=(
                None
                if value.get("open_interest", value.get("hold")) is None
                else float(value.get("open_interest", value.get("hold")))
            ),
            # Missing completion evidence is intentionally not assumed true.
            completed=value.get("completed") is True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("bar mapping is missing valid date/OHLC fields") from error


def analyze_structure(
    bars: Iterable[Bar | Mapping[str, Any]],
    *,
    tick_size: float,
    as_of_index: int | None = None,
    as_of: date | datetime | str | None = None,
    reference_price: float | None = None,
    left_window: int = 2,
    right_window: int = 2,
) -> StructureSnapshot:
    """Scanner-friendly facade supporting fetched row mappings and index replay.

    ``as_of_index`` addresses the sorted sequence of completed rows.  Slicing is
    performed before any pivot, zone, or trend calculation, so a replay cannot
    inspect later values.  It is mutually exclusive with the date cutoff.
    """

    if as_of_index is not None and as_of is not None:
        raise ValueError("as_of_index and as_of are mutually exclusive")
    normalized = [
        value if isinstance(value, Bar) else _bar_from_mapping(value)
        for value in bars
    ]
    if as_of_index is not None:
        completed = list(completed_bars_as_of(normalized))
        if as_of_index < 0 or as_of_index >= len(completed):
            raise IndexError("as_of_index is outside the completed-bar sequence")
        normalized = completed[: as_of_index + 1]
        as_of = normalized[-1].at
    return analyze_market_structure(
        normalized,
        tick_size=tick_size,
        as_of=as_of,
        reference_price=reference_price,
        left_window=left_window,
        right_window=right_window,
    )


def structural_invalidation(
    *,
    direction: TradeDirection,
    support: StructureLevel | None,
    resistance: StructureLevel | None,
    tick_size: float,
) -> float | None:
    """Place invalidation just beyond the causal structure, never by volatility."""

    if not isfinite(tick_size) or tick_size <= 0:
        raise ValueError("tick_size must be a positive finite number")
    if direction == "long":
        return None if support is None else _round_down(support.lower - tick_size, tick_size)
    if direction == "short":
        return None if resistance is None else _round_up(resistance.upper + tick_size, tick_size)
    raise ValueError("direction must be long or short")


def ratchet_invalidation(
    *,
    direction: TradeDirection,
    previous: float | None,
    candidate: float,
) -> float:
    """Adopt new confirmed structure only when it cannot widen existing risk."""

    if not isfinite(candidate) or (previous is not None and not isfinite(previous)):
        raise ValueError("invalidation prices must be finite")
    if previous is None:
        return candidate
    if direction == "long":
        return max(previous, candidate)
    if direction == "short":
        return min(previous, candidate)
    raise ValueError("direction must be long or short")


def opening_gap_quantile(
    bars: Iterable[Bar],
    *,
    probability: float = 0.95,
    minimum_observations: int = 40,
    as_of: date | datetime | str | None = None,
) -> float | None:
    """Return a nearest-rank adverse-gap context from past completed bars.

    This is an execution stress input, not a source of any structural level.
    """

    if not 0 < probability <= 1:
        raise ValueError("probability must be in (0, 1]")
    if minimum_observations < 1:
        raise ValueError("minimum_observations must be positive")
    completed = completed_bars_as_of(bars, as_of)
    gaps = sorted(
        abs(current.open - previous.close)
        for previous, current in zip(completed, completed[1:])
        if (previous.volume is None or previous.volume > 0)
        and (current.volume is None or current.volume > 0)
    )
    if len(gaps) < minimum_observations:
        return None
    rank = max(1, ceil(probability * len(gaps)))
    return gaps[rank - 1]


def evaluate_trade_geometry(
    *,
    direction: TradeDirection,
    entry: float,
    invalidation: float,
    support: StructureLevel | None,
    resistance: StructureLevel | None,
) -> TradeGeometry:
    """Fail closed unless structure gives positive ordered risk and target space."""

    if not isfinite(entry) or not isfinite(invalidation):
        raise ValueError("entry and invalidation must be finite")
    if support is None or resistance is None:
        return TradeGeometry(direction, entry, invalidation, entry, False, "support_or_resistance_unavailable")
    if direction == "long":
        target = resistance.lower
        valid = invalidation < entry < target
        reason = "ordered_long_structure" if valid else "long_structure_order_invalid"
    elif direction == "short":
        target = support.upper
        valid = target < entry < invalidation
        reason = "ordered_short_structure" if valid else "short_structure_order_invalid"
    else:
        raise ValueError("direction must be long or short")
    return TradeGeometry(direction, entry, invalidation, target, valid, reason)


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a positive finite number") from error
    if not isfinite(number) or number <= 0:
        raise ValueError(f"{field} must be a positive finite number")
    return number


def _optional_positive_number(value: Any, field: str) -> float | None:
    return None if value is None else _positive_number(value, field)


def _aware_iso_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an aware ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be an aware ISO timestamp")
    return parsed


def _validated_tick_evidence(
    technical: Mapping[str, Any],
    *,
    analysis_contract: str,
    tick_size: float,
    tick_exact: bool,
    request_started_at: Any,
    fetch_time: Any,
) -> dict[str, Any]:
    evidence = technical.get("tick_size_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("technical.tick_size_evidence is missing")
    if evidence.get("contract") != analysis_contract:
        raise ValueError("tick evidence contract does not match analysis_contract")
    if evidence.get("exact") is not tick_exact:
        raise ValueError("tick evidence exactness conflicts with technical fields")
    evidence_tick = _positive_number(
        evidence.get("tick_size"), "technical.tick_size_evidence.tick_size"
    )
    if Decimal(str(evidence_tick)) != Decimal(str(tick_size)):
        raise ValueError("tick evidence value conflicts with technical.tick_size")
    digest = evidence.get("content_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("tick evidence content_sha256 is invalid")
    retrieved = _aware_iso_timestamp(
        evidence.get("retrieved_at"), "technical.tick_size_evidence.retrieved_at"
    )
    request_started = _aware_iso_timestamp(request_started_at, "request_started_at")
    fetch_completed = _aware_iso_timestamp(fetch_time, "fetch_time")
    if retrieved < request_started or retrieved > fetch_completed:
        raise ValueError("tick evidence retrieval time is outside the fetch window")
    raw_value = evidence.get("raw_value")
    raw_match = re.search(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)",
        raw_value if isinstance(raw_value, str) else "",
    )
    if raw_match is None or Decimal(raw_match.group(0)) != Decimal(str(tick_size)):
        raise ValueError("tick evidence raw_value does not reproduce tick_size")

    if tick_exact:
        try:
            replay_exact_tick_evidence(
                evidence,
                contract=analysis_contract,
                expected_tick_size=Decimal(str(tick_size)),
            )
        except (TickEvidenceError, ValueError) as error:
            raise ValueError("exact tick evidence provenance is invalid") from error
        provider_symbol = evidence.get("provider_symbol")
        if (
            evidence.get("kind") != "actual_contract_http_detail"
            or evidence.get("hash_scope") != "http_response_body"
            or evidence.get("raw_item") != "最小变动价位"
            or evidence.get("locator")
            != "contract detail table item=最小变动价位"
            or provider_symbol
            != sina_provider_symbol_for_contract(analysis_contract)
            or evidence.get("source_url")
            != f"https://finance.sina.com.cn/futures/quotes/{provider_symbol}.shtml"
        ):
            raise ValueError("exact tick evidence provenance is invalid")
    else:
        try:
            specs_bytes = PRODUCT_SPECS_PATH.read_bytes()
            specs_hash = hashlib.sha256(specs_bytes).hexdigest()
            specs_payload = json.loads(specs_bytes)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("product specs tick proxy is unavailable") from error
        prefix_match = re.match(r"[A-Z]+", analysis_contract)
        expected_locator = (
            None
            if prefix_match is None
            else f"products[symbol={prefix_match.group(0)}].tick_size"
        )
        matching_specs = [
            item
            for item in specs_payload.get("products", [])
            if isinstance(item, Mapping)
            and prefix_match is not None
            and item.get("symbol") == prefix_match.group(0)
        ] if isinstance(specs_payload, Mapping) else []
        try:
            source_path = Path(str(evidence.get("source_path"))).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("proxy tick evidence source_path is invalid") from error
        if (
            evidence.get("kind") != "product_specs_snapshot_proxy"
            or evidence.get("provider_symbol") is not None
            or evidence.get("hash_scope") != "complete_product_specs_file"
            or evidence.get("raw_item") != "tick_size"
            or evidence.get("locator") != expected_locator
            or source_path != PRODUCT_SPECS_PATH.resolve()
            or digest != specs_hash
            or len(matching_specs) != 1
            or Decimal(str(matching_specs[0].get("tick_size")))
            != Decimal(str(tick_size))
        ):
            raise ValueError("proxy tick evidence provenance is invalid")
    return dict(evidence)


def _compact_level(level: StructureLevel | None) -> dict[str, Any] | None:
    if level is None:
        return None
    return {
        "kind": level.kind,
        "lower": level.lower,
        "center": level.center,
        "upper": level.upper,
        "strength": level.strength,
        "pivot_count": level.pivot_count,
        "touches": level.touches,
        "rejections": level.rejections,
        "crosses": level.crosses,
        "available_at": level.available_at.isoformat(),
        "last_interaction_at": (
            None
            if level.last_interaction_at is None
            else level.last_interaction_at.isoformat()
        ),
    }


def _compact_fetch_structure(
    payload: Any, *, input_snapshot_sha256: str | None = None
) -> dict[str, Any]:
    """Analyze a full ``fetch_futures --output`` payload without echoing bars."""

    if not isinstance(payload, dict):
        raise ValueError("fetch snapshot root must be a JSON object")
    if payload.get("success") is not True:
        detail = payload.get("error")
        raise ValueError(
            "fetch snapshot is unsuccessful"
            + (f": {detail}" if isinstance(detail, str) and detail else "")
        )

    main = payload.get("actual_main", payload.get("main_contract"))
    analysis = payload.get("analysis_contract", main)
    technical = payload.get("technical")
    if main is not None and not isinstance(main, dict):
        raise ValueError("actual_main/main_contract is invalid")
    if not isinstance(analysis, dict):
        raise ValueError("analysis_contract is missing or invalid")
    if not isinstance(technical, dict):
        raise ValueError("technical is missing or invalid")

    if isinstance(main, dict):
        actual_main_value = main.get("symbol")
        if not isinstance(actual_main_value, str) or not actual_main_value.strip():
            raise ValueError("main_contract.symbol is missing or invalid")
        actual_main = actual_main_value.strip().upper()
    else:
        selection = payload.get("main_selection")
        if (
            payload.get("analysis_mode") != "requested_specific_contract"
            or not isinstance(selection, Mapping)
            or selection.get("rankable_contract_count") != 0
            or selection.get("actual_main_symbol") is not None
            or selection.get("selection_claim") != "unobservable_no_rankable_row"
        ):
            raise ValueError("actual_main may be null only for an explicit contract with no rankable OI row")
        actual_main = None
    analysis_contract_value = analysis.get("symbol")
    if (
        not isinstance(analysis_contract_value, str)
        or not analysis_contract_value.strip()
    ):
        raise ValueError("analysis_contract.symbol is missing or invalid")
    analysis_contract = analysis_contract_value.strip().upper()

    technical_symbol = technical.get("symbol")
    if (
        not isinstance(technical_symbol, str)
        or technical_symbol.strip().upper() != analysis_contract
    ):
        raise ValueError("technical.symbol does not match analysis_contract")
    tick_contract = technical.get("tick_size_contract")
    if (
        not isinstance(tick_contract, str)
        or not tick_contract.strip()
        or tick_contract.strip().upper() != analysis_contract
    ):
        raise ValueError("technical.tick_size_contract does not match analysis_contract")

    tick_size = _positive_number(technical.get("tick_size"), "technical.tick_size")
    tick_exact = technical.get("tick_size_exact")
    if tick_exact not in {True, False}:
        raise ValueError("technical.tick_size_exact must be boolean")
    tick_precision = technical.get("tick_size_precision")
    expected_precision = (
        "exact_actual_contract" if tick_exact is True else "approximate_same_product_proxy"
    )
    if tick_precision not in {expected_precision, "unavailable"} or (
        tick_precision == "unavailable" and technical.get("tick_size") is not None
    ):
        raise ValueError("technical.tick_size_precision conflicts with tick exactness")
    tick_source = technical.get("tick_size_source")
    if not isinstance(tick_source, str) or not tick_source.strip():
        raise ValueError("technical.tick_size_source is missing")
    tick_evidence = _validated_tick_evidence(
        technical,
        analysis_contract=analysis_contract,
        tick_size=tick_size,
        tick_exact=tick_exact,
        request_started_at=payload.get("request_started_at"),
        fetch_time=payload.get("fetch_time"),
    )
    rows = technical.get("completed_daily_rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError(
            "technical.completed_daily_rows is missing; pass the full file written by fetch_futures.py --output"
        )

    current_price = _optional_positive_number(
        analysis.get("price"), "analysis_contract.price"
    )
    declared_completed_close = _optional_positive_number(
        technical.get("last_daily_close"), "technical.last_daily_close"
    )
    snapshot = analyze_structure(
        rows,
        tick_size=tick_size,
        reference_price=current_price or declared_completed_close,
    )

    matching_completed_rows = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("completed") is True
        and str(row.get("date", row.get("at"))) == snapshot.as_of.isoformat()
    ]
    if len(matching_completed_rows) != 1:
        raise ValueError("latest completed close cannot be identified uniquely")
    completed_close = _positive_number(
        matching_completed_rows[0].get("close"),
        "technical.completed_daily_rows latest close",
    )
    if (
        declared_completed_close is not None
        and abs(declared_completed_close - completed_close) > max(1e-9, tick_size * 1e-9)
    ):
        raise ValueError("technical.last_daily_close conflicts with completed_daily_rows")

    daily_data_evidence = technical.get("daily_data_evidence")
    if isinstance(daily_data_evidence, dict):
        bound_date = daily_data_evidence.get("bound_completed_trade_date")
        if bound_date is not None and str(bound_date) != snapshot.as_of.isoformat():
            raise ValueError(
                "technical.daily_data_evidence cutoff conflicts with completed_daily_rows"
            )
        data_evidence = daily_data_evidence
    else:
        data_evidence = {
            "status": "unverified_legacy_input",
            "quality_tier": "unknown",
            "quality_equivalence": "not_equivalent_to_verified_cache",
            "reason": "technical.daily_data_evidence is absent",
        }
    completion_evidence = payload.get("completed_trade_date_evidence")
    if isinstance(completion_evidence, dict):
        declared_cutoff = completion_evidence.get("date")
        if declared_cutoff is not None and str(declared_cutoff) != snapshot.as_of.isoformat():
            raise ValueError(
                "completed_trade_date_evidence conflicts with completed_daily_rows"
            )

    # ``analyze_structure`` needs a reference price before its returned cutoff is
    # known.  If both headline prices were absent, rerun once with the verified
    # latest completed close instead of guessing a value from incomplete input.
    reference_basis = "analysis_contract_current_price"
    if current_price is None:
        reference_basis = "latest_completed_close"
        if declared_completed_close is None:
            snapshot = analyze_structure(
                rows,
                tick_size=tick_size,
                reference_price=completed_close,
            )

    trend = snapshot.trend
    result = {
        "success": True,
        "schema_version": STRUCTURE_SCHEMA_VERSION,
        "algorithm_version": STRUCTURE_ALGORITHM_VERSION,
        "actual_main": actual_main,
        "analysis_contract": analysis_contract,
        "analysis_mode": payload.get(
            "analysis_mode",
            "product_actual_main" if analysis_contract == actual_main else "requested_specific_contract",
        ),
        "analysis_matches_actual_main": analysis_contract == actual_main,
        "fetch_time": payload.get("fetch_time"),
        "as_of": snapshot.as_of.isoformat(),
        "completed_bar_count": snapshot.completed_bar_count,
        "confirmed_pivot_count": len(snapshot.pivots),
        "reference_price": snapshot.reference_price,
        "reference_price_basis": reference_basis,
        "current_price": current_price,
        "latest_completed_close": completed_close,
        "tick_size": tick_size,
        "tick_size_contract": analysis_contract,
        "tick_size_source": tick_source.strip(),
        "tick_size_exact": tick_exact,
        "tick_size_precision": tick_precision,
        "tick_size_actionability": (
            "exact_order_compatible"
            if tick_exact is True
            else "research_only_not_for_exact_order"
        ),
        "tick_size_evidence": tick_evidence,
        "daily_data_evidence": data_evidence,
        "completed_trade_date_evidence": completion_evidence,
        "support": _compact_level(snapshot.support),
        "resistance": _compact_level(snapshot.resistance),
        "trend": {
            "direction": trend.direction,
            "side": trend.side,
            "value": trend.value,
            "raw_projection": trend.raw_projection,
            "tick_alignment": (
                "support_floor_to_tick"
                if trend.side == "support" and trend.value is not None
                else "resistance_ceil_to_tick"
                if trend.side == "resistance" and trend.value is not None
                else "not_applicable"
            ),
            "slope_per_bar": trend.slope_per_bar,
            "projected_at": trend.projected_at.isoformat(),
            "available_at": (
                None if trend.available_at is None else trend.available_at.isoformat()
            ),
            "fit_median_absolute_error": trend.fit_median_absolute_error,
            "anchor_count": len(trend.anchors),
            "reason": trend.reason,
        },
    }
    if input_snapshot_sha256 is not None:
        if re.fullmatch(r"[0-9a-f]{64}", input_snapshot_sha256) is None:
            raise ValueError("input_snapshot_sha256 must be lowercase SHA-256")
        result["input_snapshot_sha256"] = input_snapshot_sha256
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 fetch_futures.py --output 完整快照生成紧凑结构 JSON"
    )
    parser.add_argument("snapshot", type=Path, help="完整行情 JSON 文件")
    args = parser.parse_args(argv)
    try:
        snapshot_bytes = args.snapshot.expanduser().read_bytes()
        payload = json.loads(snapshot_bytes.decode("utf-8"))
        result = _compact_fetch_structure(
            payload,
            input_snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
        )
    except Exception as error:
        result = {
            "success": False,
            "schema_version": STRUCTURE_SCHEMA_VERSION,
            "algorithm_version": STRUCTURE_ALGORITHM_VERSION,
            "error": str(error),
            "warnings": ["结构输入不可验证，必须 fail closed，不得据此开仓"],
        }
    sys.stdout.write(f"{json.dumps(result, ensure_ascii=False, indent=2)}\n")
    return 0 if result["success"] else 1


__all__ = [
    "Bar",
    "BreakoutAssessment",
    "MarketStructure",
    "Pivot",
    "StructureLevel",
    "StructureSnapshot",
    "STRUCTURE_ALGORITHM_VERSION",
    "STRUCTURE_SCHEMA_VERSION",
    "TradeGeometry",
    "TrendLine",
    "analyze_market_structure",
    "analyze_structure",
    "assess_breakout",
    "build_structure_levels",
    "completed_bars_as_of",
    "derive_zone_half_width",
    "evaluate_trade_geometry",
    "find_confirmed_pivots",
    "project_trend_line",
    "opening_gap_quantile",
    "ratchet_invalidation",
    "select_strongest_levels",
    "structural_invalidation",
]


if __name__ == "__main__":
    sys.exit(main())
