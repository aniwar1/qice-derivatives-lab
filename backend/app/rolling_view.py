"""把已选研究组合与同一账户的正式日结投影为自然六个月的资金曲线。"""

from __future__ import annotations

import json
from bisect import bisect_right
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from math import isfinite
from pathlib import Path

from .agent_replay import BJT, GROUPS, digest
from .agent_replay_account import INITIAL
from .experiment_view import live_research_policy
from .study_view import BATCH, read_results

ASSUMPTIONS = [
    "北京时间向前六个自然月的同日为窗口起点，目标月无该日时取月末。以起点当日或此前最近实际收盘估值为基准，之后的交易日才计收益与交易。",
    "曲线以基准净值100、参考资金20万元重算。原账户资金与期间实际模拟盈亏另列；跨窗持仓沿原成本与估值连续计算。",
    "历史回放与接续纸面账户共用相同起点。正式曲线只收录已归档的日结；周末、休市、等待行情或盘中参考估值不生成收盘点。",
]


def window_anchor(end: date) -> date:
    month_index = end.year * 12 + end.month - 1 - 6
    year, month = divmod(month_index, 12)
    return date(year, month + 1, min(end.day, monthrange(year, month + 1)[1]))


def _signed(path: Path) -> dict:
    data = json.loads(path.read_bytes())
    if data.get("sha256") != digest({k: v for k, v in data.items() if k != "sha256"}):
        raise ValueError(f"{path.name} 档案摘要不符")
    return data


def _amount(value):
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError("账本金额无效")
    return value


def _same(a, b):
    return abs(_amount(a) - _amount(b)) <= 0.02


def _point_view(point, source, base):
    raw = _amount(point["equity"])
    ratio = raw / base if base is not None and base > 0 else None
    return {
        **point,
        "source": source,
        "raw_equity": raw,
        "equity": round(INITIAL * ratio, 2) if ratio is not None else None,
        "index_value": round(100 * ratio, 6) if ratio is not None else None,
        "return_pct": round(100 * (ratio - 1), 6) if ratio is not None else None,
    }


def _check_account(account, point):
    positions = account["positions"]
    if (
        point["open_positions"] != len(positions)
        or not _same(account["previous_equity"], point["equity"])
        or not _same(account["realized"], point["realized"])
        or not _same(
            account["cash"] + sum(_amount(p["value"]) for p in positions),
            point["equity"],
        )
        or not _same(INITIAL + point["realized"] + point["open_pnl"], point["equity"])
    ):
        raise ValueError("日结账户现金、持仓或净值无法核对")


def _archive(root, history_dir, report, seed):
    modes = (
        {g: seed["groups"][g]["variant"] for g in GROUPS}
        if seed
        else live_research_policy(root)["per_group"]
    )
    reports = {"baseline": report}
    if any(value != "baseline" for value in modes.values()):
        selection = json.loads(
            (history_dir.with_name(BATCH + "-frequency") / "replay.json").read_bytes()
        )
        if (
            selection.get("status") != "complete"
            or selection.get("settlement_revision") != "qualified-account-v2"
        ):
            raise ValueError("选定的历史组合版本尚未完成结算")
        reports.update({variant["id"]: variant for variant in selection["variants"]})
    rows = {}
    for group in GROUPS:
        plan = next(p for p in reports[modes[group]]["plans"] if p["id"] == group)
        curve, sessions = plan["curve"], plan["sessions"]
        days = [point["day"] for point in curve]
        if (
            not days
            or days != sorted(set(days))
            or days != [s["day"] for s in sessions]
        ):
            raise ValueError("历史资金曲线与交易清单日期不一致")
        if days[-1] != report["end"] or days[0] != report["start"]:
            raise ValueError("历史归档日期范围不完整")
        rows[group] = [
            (pt, s | {"source": "historical", "variant": modes[group]})
            for pt, s in zip(curve, sessions, strict=True)
        ]
    if any(
        [p["day"] for p, _ in rows[g]] != [p["day"] for p, _ in rows["A"]]
        for g in ("B", "C")
    ):
        raise ValueError("三个组合的历史估值日不一致")
    return modes, rows


def _forward(root, seed, history_end, rows, calendar, now, issues):
    accounts, recorded_at = {}, None
    if not seed:
        return accounts, recorded_at
    if seed["day"] != history_end or set(seed["groups"]) != set(GROUPS):
        raise ValueError("接续种子与历史期末或组合不匹配")
    for g in GROUPS:
        _check_account(seed["groups"][g]["account"], rows[g][-1][0])
    accounts = {g: seed["groups"][g]["account"] for g in GROUPS}
    previous = seed["sha256"]
    last_time = datetime.fromisoformat(seed["recorded_at"])
    for d in calendar[
        bisect_right(calendar, date.fromisoformat(history_end)) : bisect_right(
            calendar, now.date()
        )
    ]:
        day = d.isoformat()
        path = root / "local-data/rolling-six/settlements" / (day + ".json")
        if not path.is_file():
            break
        try:
            doc = _signed(path)
            stamp = datetime.fromisoformat(doc["recorded_at"])
            if stamp.utcoffset() is None:
                raise ValueError("日结缺少封存时区")
            if stamp > now:
                break
            if (
                doc["day"] != day
                or doc["baseline_end"] != history_end
                or doc.get("prev_sha256") != previous
                or set(doc["groups"]) != set(GROUPS)
                or stamp < last_time
                or stamp < datetime.combine(d, time(15), BJT)
            ):
                raise ValueError("日结日期、组合或前一笔摘要不一致")
            additions = {}
            for g in GROUPS:
                item = doc["groups"][g]
                if item["variant"] != seed["groups"][g]["variant"]:
                    raise ValueError("日结策略与接续种子不一致")
                if doc["decision_status"] not in (
                    "sealed_preopen",
                    "missed_preopen",
                ) or (
                    (item["decision"] is None)
                    != (doc["decision_status"] == "missed_preopen")
                ):
                    raise ValueError("日结决策与盘前封存状态不一致")
                if item["point"]["day"] != day or item["point"]["stale_marks"]:
                    raise ValueError("当日持仓估值不完整")
                _check_account(item["account"], item["point"])
                if not isinstance(item["execution"].get("exits"), list):
                    raise TypeError("日结缺少已平仓记录")
            for g in GROUPS:
                item = doc["groups"][g]
                session = {
                    "day": day,
                    "source": "forward",
                    "cutoff": doc.get("cutoff"),
                    "decision": item["decision"],
                    "execution": item["execution"],
                    "decision_status": doc["decision_status"],
                    "market_sources": doc["market_sources"],
                    "market_gaps": doc["market_gaps"],
                    "recorded_at": doc["recorded_at"],
                    "settlement_sha256": doc["sha256"],
                }
                additions[g] = (item["point"], session)
            for g, additions_for_group in additions.items():
                rows[g].append(additions_for_group)
            accounts = {g: doc["groups"][g]["account"] for g in GROUPS}
            recorded_at = doc["recorded_at"]
            previous, last_time = doc["sha256"], stamp
        except (KeyError, ValueError, OSError, TypeError) as exc:
            issues.append(f"{day} 日结未通过核对：{exc}")
            break
    return accounts, recorded_at


def _historical_positions(rows):
    held = {}
    for _, session in rows:
        execution = session["execution"]
        for close in execution["exits"]:
            held.pop(close["product"], None)
        if execution["status"] == "simulated_open":
            product = session["decision"]["product"]
            held[product] = {
                "entry_day": session["day"],
                "product": product,
                "legs": execution["legs"],
                "quantity": execution["quantity"],
                "cost": execution["premium_and_fees"],
                "marked_value": None,
                "pending_exit": None,
            }
    return list(held.values())


def _plan(group, variant, rows, anchor, today, account):
    visible = [(pt, s) for pt, s in rows if pt["day"] <= today]
    earlier = [(pt, s) for pt, s in visible if pt["day"] <= anchor]
    anchor_pt = earlier[-1] if earlier else None
    # 只有账户真正建立后才有可核验的估值。成立前的日期不补本金点。
    base = (
        _amount(anchor_pt[0]["equity"]) if anchor_pt else (INITIAL if visible else None)
    )
    points = [(pt, s) for pt, s in visible if pt["day"] > anchor]
    curve = [_point_view(pt, s["source"], base) for pt, s in points]
    sessions = [s for _, s in points]
    end = curve[-1] if curve else None
    last = visible[-1][0] if visible else None
    start_realized = _amount(anchor_pt[0]["realized"]) if anchor_pt else 0
    start_float = _amount(anchor_pt[0]["open_pnl"]) if anchor_pt else 0
    peak, drawdown = base or 0, 0
    for p in curve:
        peak = max(peak, p["raw_equity"])
        if peak > 0:
            drawdown = max(drawdown, 1 - p["raw_equity"] / peak)
    positions = (
        _historical_positions(visible)
        if account is None
        else [
            {
                "entry_day": p["day"],
                "product": p["product"],
                "legs": p["legs"],
                "quantity": p["qty"],
                "cost": round(p["cost"], 2),
                "marked_value": round(p["value"], 2),
                "pending_exit": bool(p["exit_reason"]),
            }
            for p in account["positions"]
        ]
    )
    return {
        "id": group,
        "variant": variant,
        "baseline_day": anchor_pt[0]["day"] if anchor_pt else None,
        "baseline_equity": base,
        "baseline_point": _point_view(anchor_pt[0], anchor_pt[1]["source"], base)
        if anchor_pt
        else None,
        "baseline_kind": "archived_close"
        if anchor_pt
        else ("initial_capital" if visible else "missing"),
        "latest_day": last["day"] if last else None,
        "positions": positions,
        "curve": curve,
        "sessions": sessions,
        "stats": {
            "equity": end["equity"] if end else None,
            "raw_equity": last["equity"] if last else None,
            "return_pct": end["return_pct"] if end else None,
            "net_pnl": round(end["equity"] - INITIAL, 2)
            if end and end["equity"] is not None
            else None,
            "raw_net_pnl": round(end["raw_equity"] - base, 2)
            if end and base is not None
            else None,
            "drawdown_pct": round(100 * drawdown, 6) if end and base else None,
            "opened": sum(
                s["execution"]["status"] == "simulated_open" for s in sessions
            ),
            "closed": sum(len(s["execution"]["exits"]) for s in sessions),
            "open_positions": last["open_positions"] if last else None,
            "realized_change": round(end["realized"] - start_realized, 2)
            if end
            else None,
            "open_pnl_change": round(end["open_pnl"] - start_float, 2) if end else None,
            "latest_open_pnl": last["open_pnl"] if last else None,
        },
    }


def read_rolling_baseline(
    root: Path, *, now: datetime | None = None, calendar=None
) -> dict:
    """读正式历史与连续日结。now 表示读取时点，不能取此后生成的日结。"""
    current = now or datetime.now(BJT)
    if current.utcoffset() is None:
        raise ValueError("滚动窗口须提供带时区的读取时点")
    current = current.astimezone(BJT)
    today = current.date()
    anchor = window_anchor(today)
    from .rolling_calendar import trading_days

    dates = (
        tuple(sorted(set(calendar)))
        if calendar is not None
        else tuple(date.fromisoformat(day) for day in trading_days(root))
    )
    due = today if current.time() >= time(20) else today - timedelta(days=1)
    latest_expected = (
        dates[bisect_right(dates, due) - 1] if dates and due >= dates[0] else None
    )
    known = bool(dates and dates[0] <= due <= dates[-1])
    result = {
        "status": "missing",
        "start": anchor.isoformat(),
        "end": today.isoformat(),
        "as_of": current.isoformat(),
        "historical_batch": BATCH,
        "historical_end": None,
        "latest_expected_day": latest_expected.isoformat()
        if known and latest_expected
        else None,
        "latest_day": None,
        "latest_recorded_at": None,
        "freshness": "missing",
        "missing_days": [],
        "plans": [],
        "assumptions": ASSUMPTIONS,
        "issues": [],
    }
    history = root / "local-data/agent-study" / BATCH
    report = read_results(history)
    if report["status"] != "complete":
        result["issues"].append("缺少完整的半年历史策略归档")
        return result
    result["historical_end"] = report["end"]
    issues = result["issues"]
    seed_path = root / "local-data/rolling-six/seed.json"
    seed = None
    if seed_path.is_file() and due.isoformat() >= report["end"]:
        try:
            seed = _signed(seed_path)
            sources = seed["source_sha256"]
            manifest = (history / "run-manifest.json").read_bytes()
            if (
                sha256((history / "results.json").read_bytes()).hexdigest()
                != sources["results"]
                or sha256(manifest).hexdigest() != sources["manifest"]
                or json.loads(manifest)["market_sha256"] != sources["market"]
            ):
                raise ValueError("接续种子的历史来源摘要不匹配")
            if set(seed["groups"]) != set(GROUPS) or any(
                seed["groups"][g]["variant"] not in {"baseline", "primary", "ranked"}
                for g in GROUPS
            ):
                raise ValueError("种子策略集合无效")
            created = datetime.fromisoformat(seed["recorded_at"])
            if created.utcoffset() is None:
                raise ValueError("种子缺少封存时区")
            if created > current:
                seed = None
        except (KeyError, ValueError, OSError, TypeError) as exc:
            issues.append(f"账户接续种子未通过核对：{exc}")
            seed = None
    variants, rows = _archive(root, history, report, seed)
    accounts, recorded = {}, None
    try:
        accounts, recorded = _forward(
            root, seed, report["end"], rows, dates, current, issues
        )
    except (KeyError, ValueError, OSError, TypeError) as exc:
        issues.append(f"账户接续中断：{exc}")
    rows = {
        g: [
            (pt, s)
            for pt, s in values
            if s["source"] == "forward" or pt["day"] <= due.isoformat()
        ]
        for g, values in rows.items()
    }
    plans = [
        _plan(g, variants[g], rows[g], result["start"], result["end"], accounts.get(g))
        for g in GROUPS
    ]
    latest = plans[0]["latest_day"]
    available = {pt["day"] for pt, _ in rows["A"]}
    missing = [
        d.isoformat()
        for d in dates
        if anchor < d <= due and d.isoformat() not in available
    ]
    stale_marks = [
        p["day"]
        for values in rows.values()
        for p, _ in values
        if p.get("stale_marks") and result["start"] < p["day"] <= today.isoformat()
    ]
    if not known:
        issues.append("交易日历未覆盖当前日期，最新应结算日待核")
    if any(p["baseline_day"] is None for p in plans):
        issues.append("窗口起点缺少历史收盘估值，最早只能从账户成立时的本金计算")
    if stale_marks:
        issues.append("历史曲线含缺价续用的收盘估值，详见每日 stale_marks")
    if missing and not seed_path.exists():
        issues.append("尚无已认证的现场接续种子")
    result.update(
        status="partial" if missing or issues else "ready",
        plans=plans,
        latest_day=latest,
        latest_recorded_at=recorded,
        missing_days=missing,
        freshness="calendar_unknown"
        if not known
        else (
            "missing"
            if not latest
            else ("behind" if latest < latest_expected.isoformat() else "current")
        ),
    )
    return result
