#!/usr/bin/env python3
"""Shared regular-session clock checks for provider timestamp normalization."""

from __future__ import annotations

from datetime import time
import json
from pathlib import Path
from typing import Any


def _clock_seconds(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def regular_night_end_seconds(product: str, policy_path: Path) -> int | None:
    """Return the configured regular close second, or None for no night session."""

    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        groups = policy["market_data_freshness"]["regular_session_schedule_bjt"][
            "night_end_groups"
        ]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    normalized = str(product).strip().upper()
    for label, products in groups.items():
        if not isinstance(products, list) or normalized not in products:
            continue
        if label == "NONE":
            return None
        try:
            hour_text, minute_text = str(label).split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (TypeError, ValueError):
            return None
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        return hour * 3600 + minute * 60
    return None


def is_regular_night_session_clock(
    product: str,
    clock: time,
    policy_path: Path,
) -> bool:
    """Check 21:00 through the product-specific regular night close."""

    end_seconds = regular_night_end_seconds(product, policy_path)
    if end_seconds is None:
        return False
    value = _clock_seconds(clock)
    start_seconds = 21 * 3600
    if end_seconds >= start_seconds:
        return start_seconds <= value <= end_seconds
    return value >= start_seconds or value <= end_seconds
