from __future__ import annotations

import csv
from datetime import date
from hashlib import sha256
import json
from math import isclose, isfinite
from pathlib import Path
from typing import Any


BATCH = "2026-10"
PLANS = {
    "A": {
        "name": "多因子方向买权", "style": "方向策略 · 单腿买方",
        "thesis": "在方向明确、结构压缩并有催化时，筛选权利金更有优势的单腿期权。",
        "steps": [
            ["候选排序", "冻结前一交易日数据，按方向、压缩、事前催化、低 IV 和成本门槛的命中数排序。方向代理要求至少 3 项方向条件命中，低 IV 代理要求至少 10 个此前样本且排名不高于 25%。同分看技术分。"],
            ["合约与建仓", "看多选认购、看空选认沽，从同月份前 3 档虚值里选首个预算内合约。次一交易日以公开开盘成交价模拟。"],
            ["标的失效", "若标的收盘较冻结参考价向不利方向移动至少 1 倍入场前 ATR20，且权利金低于开仓成本，则生成下一可验证开盘的退出信号。"],
        ],
        "limit": "17 笔均为强制开仓的代理样本。缺少完整历史盘口和成本优势证据，原始准入门槛还未验证。",
    },
    "B": {
        "name": "技术择强买权", "style": "方向策略 · 单腿买方",
        "thesis": "按标的方向和价格结构择强，研究技术领先的候选能否转化为期权组合收益。",
        "steps": [
            ["候选排序", "以前一交易日的标的日线计算多空分，按胜出方向的技术分、确定性分排序。"],
            ["合约与建仓", "看多选认购、看空选认沽，从同月份前 3 档虚值里选首个预算内合约。按预排顺序检验次日公开开盘成交。"],
            ["标的失效", "若标的收盘较冻结参考价向不利方向移动至少 1 倍入场前 ATR20，且权利金低于开仓成本，则生成下一可验证开盘的退出信号。"],
        ],
        "limit": "本批盘口、盘前基本面和席位证据没有完整复建。期末权益包含未平仓按市值计入的盈亏。",
    },
    "C": {
        "name": "事件驱动宽跨式", "style": "波动策略 · 同月双买",
        "thesis": "在事前可知的事件与价格压缩重叠时，以同月份两条虚值期权研究波动扩张。",
        "steps": [
            ["候选排序", "优先匹配事前已知且涉及该品种的事件，随后按区间压缩和合计权利金相对标的价格排序。"],
            ["合约与建仓", "同标的、同到期的第三档虚值认购与认沽，等手数成交。两腿都有开盘成交且均满足成交量约束才模拟建仓。"],
            ["事件失效", "有事前事件证据的组合，若两腿合计估值跌破开仓成本和上次估值，则生成下一可验证开盘的退出信号。"],
        ],
        "limit": "17 笔中只有 3 笔有事前事件证据。其余是强制双买的对照样本。日线无法验证两腿同步成交。",
    },
}


COMMON_RULES = [
    ["仓位与费用", "每组每交易日最多新建一个组合。风险预算取可用现金与 1 万元较小值，含每手每腿每边 3 元费用。手数同时受前一日最低腿成交量的 10%（向下取整，至少 1 手）和执行日最低腿成交量约束。"],
    ["权利金止损", "以最近最多 10 次可用收盘估值之间的绝对变化率中位数估算波动，少于 3 次变化时用 15%。权利金跌幅达到该波动的 3 倍时止损，跌幅阈值限制在 35% 至 65%。"],
    ["移动止盈", "权利金峰值较开仓成本上涨至少 35% 后启用。峰值回撤阈值为估计波动的 2 倍，限制在 18% 至 42%，并以覆盖双边费用的成本作为退出触发价下限。"],
]


class BacktestIntegrityError(ValueError):
    pass


def _number(value: Any) -> float:
    parsed = float(value)
    if not isfinite(parsed):
        raise BacktestIntegrityError("权益包含非有限数字")
    return parsed


def _equal(actual: float, expected: float) -> None:
    if not isclose(actual, expected, rel_tol=1e-9, abs_tol=0.011):
        raise BacktestIntegrityError("回测权益与逐日记录不一致")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_backtest(directory: Path) -> dict:
    """只展示与原始市场快照和逐日账目一致的历史曲线。"""
    plans = [{"id": key, **value, "steps": [*value["steps"], *COMMON_RULES]} for key, value in PLANS.items()]
    if not directory.is_dir():
        return {"status": "missing", "batch": BATCH, "plans": plans}
    try:
        return _reconcile(directory, plans)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BacktestIntegrityError("本地回测资料缺失或未通过一致性校验") from exc


def _reconcile(directory: Path, plans: list) -> dict:
    summary = _read_json(directory / "summary.json")
    manifest = _read_json(directory / "source-manifest.json")
    market_digest = sha256((directory / "market.json.gz").read_bytes()).hexdigest()
    if market_digest != manifest["data_sha256"]:
        raise BacktestIntegrityError("回测市场输入摘要不符")
    initial = _number(summary["initial_cash_each"])
    cap = _number(summary["daily_new_cost_cap_each"])
    if initial <= 0 or not 0 < cap <= initial:
        raise BacktestIntegrityError("回测资金口径无效")
    with (directory / "equity.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    points = {(date.fromisoformat(row["day"]).isoformat(), row["group"]): row for row in rows}
    days = sorted({day for day, _ in points})
    if len(points) != len(rows) or set(points) != {(day, group) for day in days for group in PLANS}:
        raise BacktestIntegrityError("交易日与组合存在缺口或重复数据")
    if not days or len(days) != summary["sessions_processed"] or days[-1] != summary["through"]:
        raise BacktestIntegrityError("回测期与摘要不一致")
    reports = {day: _read_json(directory / "days" / f"{day}.json") for day in days}
    groups = []
    for plan in plans:
        group = plan["id"]
        curve, sessions = [], []
        peak, drawdown, opened, closed = initial, 0.0, 0, 0
        for day in days:
            report = reports[day]
            information_day = date.fromisoformat(report["previous_information_date"]).isoformat()
            if report["official_trading_day"] != day or information_day >= day:
                raise BacktestIntegrityError("逐日计划没有锁定到前一交易日")
            entry, account, exits = (report["groups"][group][key] for key in ("new_entry", "closing_account", "exits"))
            row = points[day, group]
            equity = _number(row["equity"])
            realized = _number(account["realized_net_pnl"])
            open_pnl = _number(account["open_mark_pnl"])
            _equal(equity, _number(account["total_equity"]))
            _equal(equity, initial + realized + open_pnl)
            _equal(_number(row["cash"]), _number(account["cash"]))
            opening = _number(row["new_entry_cost"])
            if not 0 <= opening <= cap:
                raise BacktestIntegrityError("开仓超出历史组合设定的预算")
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
            curve.append({"day": day, "equity": equity, "return_pct": (equity / initial - 1) * 100,
                          "realized": realized, "open_pnl": open_pnl, "open_positions": int(row["open_positions"])})
            trades = [{"trade_id": item["trade_id"], "net_pnl": _number(item["net_pnl"]),
                       "reason": item["exit_signal"]["reason"]}
                      for item in exits if item["status"] == "closed"]
            closed += len(trades)
            outcome = {"status": entry["status"], "reason": entry.get("reason")}
            if entry["status"] == "opened":
                opened += 1
                pick = entry["plan"]
                if pick["decision_cutoff_day"] != information_day:
                    raise BacktestIntegrityError("候选计划与当日信息截止不一致")
                _equal(_number(entry["paid"]), opening)
                if event := pick.get("event"):
                    outcome["event"] = {key: event[key] for key in ("title", "release_bjt", "preannounced_source", "transmission")}
                outcome.update(
                    product=pick["product"], contract=pick["underlying"], direction=pick["direction"] if group != "C" else None,
                    decision=entry["decision"], candidate_count=int(entry["candidate_count"]),
                    quantity=int(entry["quantity_per_leg"]), paid=opening, fee=_number(entry["fee"]),
                    reference=_number(pick["reference"]),
                    legs=[{"symbol": leg["symbol"], "open_price": _number(leg["open_price"])} for leg in entry["entry_legs"]],
                )
            sessions.append({"day": day, "information_day": information_day, "outcome": outcome, "exits": trades})
        stats = summary["groups"][group]
        _equal(curve[-1]["equity"], _number(stats["equity"]))
        _equal(curve[-1]["realized"], _number(stats["realized"]))
        _equal(curve[-1]["open_pnl"], _number(stats["open_pnl"]))
        _equal(drawdown * 100, _number(stats["drawdown"]) * 100)
        if opened != stats["opened"] or closed != stats["closed"] or opened - closed != stats["open_count"]:
            raise BacktestIntegrityError("开平仓数量与摘要不符")
        groups.append({**plan, "curve": curve, "sessions": sessions, "stats": {
            "opened": opened, "closed": closed, "open_positions": stats["open_count"],
            "equity": curve[-1]["equity"], "return_pct": curve[-1]["return_pct"], "drawdown_pct": drawdown * 100,
            "realized": curve[-1]["realized"], "open_pnl": curve[-1]["open_pnl"],
        }})
    universe = manifest["configured_universe"]
    return {"status": "ready", "batch": BATCH, "plans": groups, "days": days,
            "initial_cash": initial, "daily_cap": cap, "fee_per_side": 3,
            "covered_products": sorted(symbol for symbol, exchange in universe.items()
                                       if exchange in manifest["known_complete_exchanges"]),
            "configured_count": len(universe), "market_sha256": market_digest}
