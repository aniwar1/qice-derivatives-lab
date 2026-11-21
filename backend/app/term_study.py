"""在观察日的真实合约链中匹配主力月与远月宽跨，并按冻结数量比较成交代理。"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from math import floor
from statistics import mean, median
from typing import Any

from .agent_replay import FUTURE, LEG, positive

RISK_BUDGET = 2000.0
SLIPPAGE = 0.01
FEE = 3.0
TARGET_OTM_PCT = 2.0
MATCH_MAX_PP = 1.0


def _delivery(contract):
    digits = FUTURE.fullmatch(contract)[2]
    if len(digits) != 4:
        return None
    try:
        return date(2000 + int(digits[:2]), int(digits[2:]), 1)
    except ValueError:
        return None


def _leg_choices(contract, price, options):
    choices = {"C": [], "P": []}
    for symbol, row in options.items():
        match = LEG.fullmatch(symbol)
        if not match or match[1] + match[2] != contract:
            continue
        if not (positive(row[3]) and positive(row[4]) and row[4] >= 100 and positive(row[5])):
            continue
        side, strike = match[3], float(match[4])
        distance = 100 * (strike / price - 1) * (1 if side == "C" else -1)
        if not 0.5 <= distance <= 5.0:
            continue
        choices[side].append({"id": symbol, "side": side, "strike": strike, "premium": row[3],
                              "volume": row[4], "open_interest": row[5], "otm_pct": round(distance, 6),
                              "iv_pct": row[6] if len(row) > 6 and positive(row[6]) else None})
    return choices


def _structure(contract, future, options, as_of, exit_day, multiplier, budget, reference=None, expiries=None):
    delivery = _delivery(contract)
    if not delivery:
        return None, "unverified_delivery_month"
    cutoff_proxy = (delivery - timedelta(days=1)).replace(day=1)
    if expiries is None and FUTURE.fullmatch(contract)[1] in {"AP", "CJ"}:
        cutoff_proxy = (cutoff_proxy - timedelta(days=1)).replace(day=1)
    if exit_day >= cutoff_proxy:
        return None, "expiry_buffer"
    choices = _leg_choices(contract, future[3], options)
    legs = []
    for index, side in enumerate(("C", "P")):
        pool = choices[side]
        if not pool:
            return None, "illiquid_or_missing_otm_leg"
        if expiries is not None:
            pool = [leg for leg in pool if expiries.get(leg["id"]) and expiries[leg["id"]]["expiry"] > exit_day.isoformat()]
            if not pool:
                return None, "expiry_missing_or_before_exit"
            for leg in pool:
                info = expiries[leg["id"]]
                leg.update(expiry=info["expiry"], expiry_source_sha256=info["sha256"], expiry_available_at=info["available_at"])
        target = reference[index]["otm_pct"] if reference else TARGET_OTM_PCT
        if reference:
            pool = [leg for leg in pool if abs(leg["otm_pct"] - target) <= MATCH_MAX_PP]
            if not pool:
                return None, "unmatched_moneyness"
        legs.append(min(pool, key=lambda leg: (abs(leg["otm_pct"] - target), -leg["volume"], leg["id"])))
    unit_debit = sum(leg["premium"] for leg in legs) * multiplier * (1 + SLIPPAGE) + 2 * FEE
    # 同时预留平仓两腿手续费，即使权利金全部损失也不穿透风险预算。
    qty = floor(min(budget / (unit_debit + 2 * FEE), *(leg["volume"] * 0.01 for leg in legs)))
    if qty <= 0:
        return None, "insufficient_prior_budget"
    return {"contract": contract, "underlying_price": future[3], "future_open_interest": future[5],
            "future_volume": future[4], "legs": legs, "month_start_proxy": delivery.isoformat(),
            "expiry_buffer_boundary": cutoff_proxy.isoformat(),
            "days_to_buffer": (cutoff_proxy - as_of).days, "planned_quantity": qty,
            "prior_unit_debit": round(unit_debit, 4)}, None


def option_leader(product, options):
    # 汇总该品种全部行权价的看涨和看跌持仓，持仓并列时按成交量和月份排序。
    totals = {}
    for symbol, row in options.items():
        match = LEG.fullmatch(symbol)
        if match and match[1] == product and positive(row[5]):
            total = totals.setdefault(match[1] + match[2], [0, 0])
            total[0] += row[5]
            total[1] += row[4] if positive(row[4]) else 0
    return min(totals, key=lambda k: (-totals[k][0], -totals[k][1], k), default=None)


def select_term_pair(product, futures, options, as_of, exit_day, multiplier, *, risk_budget=RISK_BUDGET, anchor="future", expiries=None):
    """按观察日持仓选主力，按月从近到远寻找可同风险比较的两腿合约。"""
    if exit_day < as_of or not positive(multiplier) or not positive(risk_budget):
        raise ValueError("日期、乘数或预算无效")
    contracts = {}
    for symbol, row in futures.items():
        match = FUTURE.fullmatch(symbol)
        if match and match[1] == product and all(positive(row[i]) for i in (3, 4, 5)):
            contracts[symbol] = row
    if not contracts:
        return None, "no_underlying_quotes"
    if anchor == "option":
        main = option_leader(product, options)
        if main not in contracts:
            return None, "option_leader_missing_underlying"
    elif anchor == "future":
        main = min(contracts, key=lambda contract: (-contracts[contract][5], contract))
    else:
        raise ValueError("主力月份口径无效")
    base, reason = _structure(main, contracts[main], options, as_of, exit_day, multiplier, risk_budget, expiries=expiries)
    if not base:
        return None, "main_" + reason
    distant = sorted(k for k in contracts if _delivery(k) and _delivery(k) > _delivery(main))
    failures = []
    for contract in distant:
        far, reason = _structure(contract, contracts[contract], options, as_of, exit_day, multiplier,
                                 risk_budget, base["legs"], expiries)
        if far:
            return {"main": base, "far": far, "earlier_far_rejections": failures}, None
        failures.append({"contract": contract, "reason": reason})
    return None, "far_" + (failures[0]["reason"] if failures else "no_later_month")


def execution(structure, entry, exit_, multiplier, budget=RISK_BUDGET):
    """以日开盘代理评估预先冻结的整数手数，记录双腿不同步和盘口缺失边界。"""
    qty = structure["planned_quantity"]
    symbols = [leg["id"] for leg in structure["legs"]]
    result: dict[str, Any] = {}
    for name, quotes in (("entry", entry), ("exit", exit_)):
        bars = [quotes.get(symbol) for symbol in symbols]
        if any(not row or not positive(row[0]) or not positive(row[4]) or row[4] < qty for row in bars):
            return {**result, "status": f"{name}_quote_or_volume_gap"}
        premiums = [row[0] for row in bars]
        gross = qty * multiplier * sum(premiums)
        cash = gross * (1 + SLIPPAGE) + qty * 2 * FEE if name == "entry" else gross * (1 - SLIPPAGE) - qty * 2 * FEE
        result[name + "_premiums"] = premiums
        result[name + "_cash"] = cash
        if name == "entry" and cash + qty * 2 * FEE > budget:
            return {**result, "status": "entry_debit_exceeds_budget"}
    pnl = result["exit_cash"] - result["entry_cash"]
    return {**result, "status": "priced", "pnl_yuan": round(pnl, 4),
            "budget_return_pct": round(100 * pnl / budget, 5),
            "premium_return_pct": round(100 * pnl / result["entry_cash"], 5)}


def summarize(rows):
    clusters = defaultdict(list)
    for row in rows:
        clusters[row["event_at"]].append(row)
    main = [mean(row["main"]["execution"]["budget_return_pct"] for row in group) for group in clusters.values()]
    far = [mean(row["far"]["execution"]["budget_return_pct"] for row in group) for group in clusters.values()]
    differences = [b - a for a, b in zip(main, far)]
    omitted = [mean(differences[:i] + differences[i+1:]) for i in range(len(differences))] if len(differences) > 1 else []
    return {"paired_products": len(rows), "unique_products": len({r["product"] for r in rows}),
            "event_clusters": len(clusters), "main_mean_return_pct": round(mean(main), 5) if main else None,
            "far_mean_return_pct": round(mean(far), 5) if far else None,
            "far_minus_main_pp": round(mean(differences), 5) if differences else None,
            "median_difference_pp": round(median(differences), 5) if differences else None,
            "leave_one_event_out_range_pp": [round(min(omitted), 5), round(max(omitted), 5)] if omitted else None,
            "far_wins": sum(d > 1e-7 for d in differences), "main_wins": sum(d < -1e-7 for d in differences),
            "ties": sum(abs(d) <= 1e-7 for d in differences)}
