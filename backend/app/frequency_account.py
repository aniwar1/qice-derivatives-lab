"""在查看下一交易日开盘之前，按冻结委托顺序与真实组合状态确定唯一候选。"""

from math import floor

from .agent_replay import GROUPS, validate_decision
from .agent_replay_account import (
    FEE,
    INITIAL,
    SLIPPAGE,
    execution_terms,
    strangle_scenarios,
)

WAIT = {
    "action": "wait",
    "product": "",
    "legs": [],
    "evidence_ids": [],
    "technical": "组合条件筛查后等待",
    "fundamentals": "见当日模型原始审查",
    "news": "见当日模型原始审查",
    "ownership": "自营未知",
    "thesis": "没有通过账户门槛的候选",
    "invalidation": "下次冻结时点重新判定",
    "risk_bps": 0,
    "holding_days": 0,
    "invalidation_price": 0,
    "scenario_target": 0,
    "take_profit_close": 0,
    "max_lots": 0,
    "max_entry_premium": 0,
    "scenario_low": 0,
    "scenario_high": 0,
}


def account_state(account):
    return {
        "cash": round(account.cash, 2),
        "equity": round(account.previous_equity, 2),
        "open_cost": round(sum(p["cost"] for p in account.positions), 2),
        "positions": [
            {
                "product": p["product"],
                "quantity": p["qty"],
                "pending_exit": bool(p["exit_reason"]),
            }
            for p in account.positions
        ],
    }


def assess(group, candidate, packet, account):
    checks = {g: WAIT for g in GROUPS} | {group: candidate}
    try:
        validate_decision(checks, packet)
    except (KeyError, TypeError, ValueError) as exc:
        return {"eligible": False, "reason": f"冻结证据或策略条件不足：{exc}"}
    if candidate["action"] == "wait":
        return {"eligible": False, "reason": "Agent 明确等待"}
    product = packet["products"][candidate["product"]]
    quotes = {leg["id"]: leg for leg in product["options"]}
    legs = [quotes[key] for key in candidate["legs"]]
    terms = execution_terms(group, candidate, packet)
    budget = min(
        account.cash,
        account.previous_equity * terms["effective_risk_bps"] / 10000,
        max(
            0,
            account.previous_equity * 0.08
            - sum(pos["cost"] for pos in account.positions),
        ),
    )
    quote = sum(leg["premium"] for leg in legs)
    # 买入限价低于前收时可挂条件单，实际整手检查仍由下个开盘价代理完成。
    estimate = min(quote, candidate["max_entry_premium"] or quote)
    cost = estimate * product["multiplier"] * (1 + SLIPPAGE) + len(legs) * FEE
    volume_lots = floor(min(leg["volume"] for leg in legs) * 0.01)
    lots = min(floor(budget / cost), volume_lots)
    if terms["size_limit_lots"] is not None:
        lots = min(lots, terms["size_limit_lots"])
    details = {
        "estimated_lots": lots,
        "estimated_unit_cost": round(cost, 2),
        "effective_risk_bps": terms["effective_risk_bps"],
        "budget_cny": round(budget, 2),
    }
    if group == "C":
        stress = strangle_scenarios(
            product, legs, [leg["premium"] for leg in legs], candidate
        )
        if not stress["eligible"]:
            return details | {
                "eligible": False,
                "reason": "收盘双腿压力成本高于两尾情景价值",
                "stress": stress,
            }
    if "execution_expiries" in packet:
        formula = packet["execution_expiries"].get(product["technical"]["contract"])
        known = packet.get("execution_leg_expiries", {})
        sources = {
            leg["id"]: known.get(leg["id"])
            or {"expiry": formula, "method": "published_formula"}
            for leg in legs
        }
        details["expiry_sources"] = sources
        if any(not src["expiry"] for src in sources.values()):
            return details | {
                "eligible": False,
                "reason": "截止时无法核实合约到期日，持有窗口未确认",
            }
        expiry = min(src["expiry"] for src in sources.values())
        exit_day = packet["execution_sessions"][candidate["holding_days"]]
        details.update(verified_last_trade=expiry, planned_exit=exit_day)
        if exit_day > expiry:
            return details | {
                "eligible": False,
                "reason": "计划最迟平仓超过经核实的合约到期日",
            }
    if volume_lots == 0:
        return details | {"eligible": False, "reason": "前日可参与成交量不足一手"}
    if any(p["product"] == candidate["product"] for p in account.positions):
        return details | {
            "eligible": False,
            "reason": "截止时同品种已有仓位",
            "reserve_allowed": True,
        }
    return details | {
        "eligible": lots >= 1,
        "reason": "截止时可提交条件单" if lots >= 1 else "账户现金或风险预算不足一手",
        "reserve_allowed": lots < 1,
    }


def select_order(group, pair, packet, account, depth):
    trace = []
    for rank, key in enumerate(("primary", "reserve")[:depth], 1):
        candidate = pair[key]
        if candidate is None:
            break
        if key == "reserve" and (
            pair["primary"]["action"] != "enter"
            or candidate["action"] != "enter"
            or pair["primary"]["product"] == candidate["product"]
        ):
            trace.append(
                {
                    "rank": rank,
                    "eligible": False,
                    "reason": "备选必须独立且首选已有入场意向",
                }
            )
            break
        eligibility = assess(group, candidate, packet, account)
        trace.append({"rank": rank, "product": candidate["product"], **eligibility})
        if eligibility["eligible"]:
            return candidate, rank, trace
        if candidate["action"] == "wait":
            return candidate, None, trace
        if not eligibility.get("reserve_allowed"):
            break
    return WAIT, None, trace


def segment(curve, sessions, start, end):
    opening = curve[start - 1]["equity"] if start else INITIAL
    peak = opening
    dd = 0
    for point in curve[start:end]:
        peak = max(peak, point["equity"])
        dd = max(dd, 1 - point["equity"] / peak)
    equity = curve[end - 1]["equity"] if end > start else opening
    entries = sum(
        s["execution"]["status"] == "simulated_open" for s in sessions[start:end]
    )
    exits = sum(len(s["execution"]["exits"]) for s in sessions[start:end])
    return {
        "start": curve[start]["day"] if end > start else None,
        "end": curve[end - 1]["day"] if end > start else None,
        "start_equity": opening,
        "equity": equity,
        "net_pnl": round(equity - opening, 2),
        "return_pct": round(100 * (equity / opening - 1), 6),
        "drawdown_pct": round(100 * dd, 6),
        "opened": entries,
        "closed": exits,
    }


def prefer(new, reference):
    return (
        new["stats"]["net_pnl"] > reference["stats"]["net_pnl"]
        and new["segments"]["last42"]["net_pnl"]
        > reference["segments"]["last42"]["net_pnl"]
        and new["stats"]["opened"] > reference["stats"]["opened"]
        and new["stats"]["drawdown_pct"] <= reference["stats"]["drawdown_pct"] + 0.5
    )
