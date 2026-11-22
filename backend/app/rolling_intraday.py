"""用仍在交易中的真实期权盘口，对最近一次已结算的持仓单独估值。"""

from bisect import bisect_right
from datetime import date, datetime, time, timedelta
from functools import lru_cache
import json
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .live_options import OPTION_SYMBOL, QUOTE_LINE, TICK_URL
from .rolling_calendar import trading_days

BJT = ZoneInfo("Asia/Shanghai")
POLICY = Path(__file__).resolve().parents[2] / "config/futures_main_report_policy.json"


@lru_cache(maxsize=1)
def _schedule():
    cfg = json.loads(POLICY.read_bytes())["market_data_freshness"][
        "regular_session_schedule_bjt"
    ]
    daylight = [
        (time.fromisoformat(a), time.fromisoformat(b)) for a, b in cfg["day_intervals"]
    ]
    night = {
        p: time.fromisoformat(end)
        for end, products in cfg["night_end_groups"].items()
        if end != "NONE"
        for p in products
    }
    return daylight, night


def active_date(product, at, days):
    daylight, night = _schedule()
    today, clock = at.date(), at.time()
    if any(a <= clock < b for a, b in daylight):
        return today.isoformat() if today.isoformat() in days else None
    end = night.get(product)
    if end is None:
        return None
    if clock >= time(21) and (end < time(8) or clock < end):
        eve = today
    elif clock < end < time(8):
        eve = today - timedelta(days=1)
    else:
        return None
    key = eve.isoformat()
    i = bisect_right(days, key)
    if not i or days[i - 1] != key or i == len(days):
        return None
    next_day = date.fromisoformat(days[i])
    # 普通周五夜盘归入下周一。连休前不根据时钟模板认定开盘。
    if (next_day - eve).days == 1 or (
        eve.weekday() == 4 and (next_day - eve).days == 3
    ):
        return days[i]
    return None


def read_quotes(symbols):
    providers = {}
    for key in symbols:
        match = OPTION_SYMBOL.fullmatch(key)
        if not match:
            continue
        product, month, side, strike = match.groups()
        prefix = product.lower() + month
        for name in (f"{prefix}{side}{strike}", f"{prefix}-{side}-{strike}"):
            providers[name] = key
    result = {}
    if not providers:
        return result
    response = requests.get(
        TICK_URL + ",".join("P_OP_" + s for s in providers),
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=(2, 4),
    )
    response.raise_for_status()
    for name, body in QUOTE_LINE.findall(response.content.decode("gb18030")):
        key = providers.get(name)
        if not key:
            continue
        try:
            row = body.split(",")
            bid, ask = float(row[1]), float(row[3])
            stamp = datetime.strptime(row[32], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
            if (
                float(row[7]) != float(OPTION_SYMBOL.fullmatch(key)[4])
                or not (0 < bid <= ask and isfinite(ask))
                or min(int(row[0]), int(row[4])) < 1
            ):
                continue
        except (TypeError, ValueError, IndexError):
            continue
        if key not in result or stamp > result[key][0]:
            result[key] = (stamp, (bid + ask) / 2)
    return result


def read_intraday(
    root: Path, *, now=None, snapshot_loader=None, quote_loader=read_quotes
):
    from .rolling_update import latest_account_snapshot

    pinned_time = now is not None
    now = (now or datetime.now(BJT)).astimezone(BJT)
    result = {
        "status": "unavailable",
        "base_day": None,
        "observed_at": None,
        "groups": [],
        "note": "等待可核对的日结账户。",
    }
    saved = (snapshot_loader or latest_account_snapshot)(root)
    if not saved:
        return result
    days = trading_days(root)
    base = saved["day"]
    result["base_day"] = base
    if now.date().isoformat() > days[-1]:
        return result | {"note": "交易日历待更新。"}
    # 铜覆盖配置范围中最长的夜盘时段，持仓腿仍须分别核对品种交易时段。
    session = active_date("CU", now, days)
    if not session:
        return result | {
            "status": "closed",
            "note": "休市或盘间休息，正式曲线采用最近一次日结。",
        }
    i = bisect_right(days, base)
    if i >= len(days) or days[i] != session:
        return result | {"note": "等待相邻交易日的已核实账户，暂不计算盘中浮动。"}
    accounts = {g: saved["groups"][g]["account"]["positions"] for g in "ABC"}
    needed = {
        leg
        for positions in accounts.values()
        for p in positions
        if active_date(p["product"], now, days) == session
        for leg in p["legs"]
    }
    try:
        quotes = quote_loader(needed) if needed else {}
    except (requests.RequestException, UnicodeError, ValueError):
        quotes = {}
    if not pinned_time:
        now = datetime.now(BJT)
    stamps = []
    for g, positions in accounts.items():
        total, marked, delta = 0, 0, 0.0
        for p in positions:
            prior, value, hits = p["value"], 0.0, 0
            for leg in p["legs"]:
                total += 1
                quote = quotes.get(leg)
                if not quote:
                    continue
                stamp, mid = quote
                if (
                    not now - timedelta(minutes=2) <= stamp <= now
                    or active_date(p["product"], stamp, days) != session
                    or active_date(p["product"], now, days) != session
                ):
                    continue
                hits += 1
                stamps.append(stamp)
                value += mid * p["mult"] * p["qty"]
            marked += hits
            if hits == len(p["legs"]):
                delta += value - prior
        result["groups"].append(
            {
                "id": g,
                "total_legs": total,
                "marked_legs": marked,
                "pnl_change": round(delta, 2) if total == marked else None,
            }
        )
    if not any(g["total_legs"] for g in result["groups"]):
        return result | {
            "status": "flat",
            "note": "上次日结三个组合均无持仓。当日新成交待日结确认。",
        }
    complete = all(g["marked_legs"] == g["total_legs"] for g in result["groups"])
    status = "live" if complete else "partial" if stamps else "unavailable"
    return result | {
        "status": status,
        "observed_at": min(stamps).isoformat() if stamps else None,
        "note": "按两分钟内双边盘口中点对上次结算持仓估值。缺报价的组合暂缺数值，当日新成交待日结确认。",
    }
