"""让基线计算、盘中报价与日结生产者共享同一交易日历。"""

import json
import logging
from datetime import date, timedelta
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

import requests

from scripts.agent_replay import atomic

CALENDAR_URL = (
    "https://raw.githubusercontent.com/akfamily/akshare/main/akshare/data/calendar.json"
)


def _dates(rows):
    if not isinstance(rows, list):
        raise TypeError("交易日列表格式错误")
    days = tuple(date.fromisoformat(d).isoformat() for d in rows)
    if not days or days != tuple(sorted(set(days))):
        raise ValueError("交易日存在重复或顺序错误")
    return days


@lru_cache(maxsize=1)
def _bundled():
    from akshare.futures.cons import get_calendar

    return _dates(get_calendar())


def _digest(days):
    return sha256("\n".join(days).encode()).hexdigest()


def trading_days(root: Path):
    bundled = _bundled()
    file = root / "local-data/rolling-six/calendar.json"
    if not file.exists():
        return bundled
    record = json.loads(file.read_bytes())
    days = _dates(record["days"])
    if record["source"] != CALENDAR_URL or _digest(days) != record["sha256"]:
        raise ValueError("交易日历摘要或来源不符")
    return days if days[-1] >= bundled[-1] else bundled


def refresh_calendar(root: Path, today: date):
    days = trading_days(root)
    if date.fromisoformat(days[-1]) - today > timedelta(days=35):
        return
    folder = root / "local-data/rolling-six"
    attempt = folder / "calendar-check.json"
    if (
        attempt.exists()
        and json.loads(attempt.read_bytes()).get("day") == today.isoformat()
    ):
        return
    status = "no_extension"
    try:
        response = requests.get(CALENDAR_URL, timeout=(3, 7))
        response.raise_for_status()
        new_days = _dates(response.json())
        if new_days[-1] > days[-1]:
            # 不重写已承接的半年账目。上游的新交易日按日期追加。
            extension = tuple(d for d in new_days if d > days[-1])
            overlap = [d for d in days if days[-1][:4] == d[:4]]
            if not set(overlap).issubset(new_days):
                raise ValueError("上游年末日历与本地覆盖不一致")
            merged = days + extension
            atomic(
                folder / "calendar.json",
                {
                    "source": CALENDAR_URL,
                    "retrieved_on": today.isoformat(),
                    "sha256": _digest(merged),
                    "days": merged,
                },
            )
            status = "extended"
    except (requests.RequestException, TypeError, ValueError) as error:
        status = "unavailable"
        logging.getLogger(__name__).warning(
            "交易日历暂未更新：%s", type(error).__name__
        )
    atomic(attempt, {"day": today.isoformat(), "status": status})
