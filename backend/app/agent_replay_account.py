"""逐笔回放收盘信号与下个开盘价代理，保持权益、模型意见和成交质量可分辨。"""

from __future__ import annotations

from math import floor

from .agent_replay import positive, stressed_profit

INITIAL = 200_000.0
FEE = 3.0
SLIPPAGE = 0.01


def strangle_scenarios(analysis, legs, premiums, decision):
    low, high = analysis["technical"]["low20"], analysis["technical"]["high20"]
    if not (positive(low) and positive(high) and high > low):
        return {"eligible": False, "reason": "缺少可复核的前 20 日实盘价格区间"}
    mult = analysis["multiplier"]

    def net(target):
        return round(
            sum(
                stressed_profit(target, leg, premium, mult)
                for leg, premium in zip(legs, premiums, strict=True)
            ),
            4,
        )

    lower = decision["scenario_low"]
    upper = decision["scenario_high"]
    downside, upside = net(lower), net(upper)
    return {
        "scenario_low": lower,
        "scenario_high": upper,
        "scenario_source": "agent",
        "eligible": max(downside, upside) > 0,
        "low20": low,
        "high20": high,
        "downside_net_cny_per_pair": downside,
        "upside_net_cny_per_pair": upside,
        "roundtrip_fees_cny": FEE * 2 * len(legs),
        "premium_cny_per_pair": round(sum(premiums) * mult, 4),
        "slippage_pct_each_side": 2,
    }


def execution_terms(group, decision, packet):
    product = decision["product"]
    dimensions = {
        d["dimension"]
        for d in packet["documents"]
        if d["id"] in decision["evidence_ids"]
        and product in d["products"]
        and d.get("freshness_role") != "low_frequency_background_only"
    }
    missing = sorted({"fundamentals", "news"} - dimensions)
    agent_budget = decision["risk_bps"]
    effective = min(agent_budget, 50) if group == "B" and missing else agent_budget
    return {
        "agent_risk_bps": agent_budget,
        "effective_risk_bps": effective,
        "budget_reason": "B 已引用的近期基本面或消息有缺口，研究预算上限 50 基点"
        if group == "B" and missing
        else "采用 Agent 预算",
        "missing_cited_dimensions": missing,
        "target_exit_close": decision["take_profit_close"] or None,
        "size_limit_lots": decision["max_lots"] or None,
        "opening_premium_limit": decision["max_entry_premium"] or None,
        "order_terms_source": "structured_agent_fields",
        "text_conditions_note": "执行模型给出的止盈、失效、期限、手数和权利金上限。收盘触发后按次一可用开盘平仓。",
    }


class Account:
    def __init__(self, group="B"):
        self.group = group
        self.cash = INITIAL
        self.positions = []
        self.opened = self.closed = 0
        self.realized = self.drawdown = 0.0
        self.peak = INITIAL
        self.previous_equity = INITIAL

    def process(self, day, decision, packet, prices, delayed_futures=()):
        # 退出只依赖上次收盘已经生成的信号。
        exits = []
        for pos in list(self.positions):
            if not pos["exit_reason"]:
                continue
            rows = [prices["option"].get(leg) for leg in pos["legs"]]
            if any(
                not row
                or not positive(row[0])
                or not positive(row[4])
                or row[4] < pos["qty"]
                for row in rows
            ):
                continue
            proceeds = pos["qty"] * (
                sum(r[0] for r in rows) * pos["mult"] * (1 - SLIPPAGE) - FEE * len(rows)
            )
            pnl = proceeds - pos["cost"]
            self.cash += proceeds
            self.realized += pnl
            self.closed += 1
            self.positions.remove(pos)
            exits.append(
                {
                    "product": pos["product"],
                    "entry_day": pos["day"],
                    "legs": pos["legs"],
                    "quantity": pos["qty"],
                    "multiplier": pos["mult"],
                    "raw_open_prices": [r[0] for r in rows],
                    "proceeds_after_costs": round(proceeds, 2),
                    "opening_debit": round(pos["cost"], 2),
                    "pnl": round(pnl, 2),
                    "reason": pos["exit_reason"],
                }
            )
        terms = (
            execution_terms(self.group, decision, packet)
            if decision["action"] == "enter"
            else {}
        )
        execution = self.open(day, decision, packet, prices, terms) | terms
        execution["exits"] = exits
        not_fresh = []
        for pos in self.positions:
            option_rows = [prices["option"].get(leg) for leg in pos["legs"]]
            pos["age"] += 1
            if all(r and positive(r[3]) and positive(r[4]) for r in option_rows):
                pos["value"] = pos["qty"] * pos["mult"] * sum(r[3] for r in option_rows)
            else:
                not_fresh.append(pos["product"])
            # 延迟公布的收盘原件只用于事后估值，不能在当晚触发次日交易。
            exchange = packet["products"][pos["product"]]["exchange"]
            future = (
                None
                if exchange in delayed_futures
                else prices["future"].get(pos["underlying"])
            )
            breached = (
                future
                and positive(future[3])
                and (
                    (pos["side"] == "C" and future[3] <= pos["invalid"])
                    or (pos["side"] == "P" and future[3] >= pos["invalid"])
                )
            )
            if not pos["exit_reason"]:
                if breached:
                    pos["exit_reason"] = "标的收盘触及 Agent 失效位"
                elif (
                    future
                    and positive(future[3])
                    and pos["target_exit"] is not None
                    and (
                        (pos["side"] == "C" and future[3] >= pos["target_exit"])
                        or (pos["side"] == "P" and future[3] <= pos["target_exit"])
                    )
                ):
                    pos["exit_reason"] = "标的收盘达到 Agent 明示止盈价"
                elif pos["age"] >= pos["hold"]:
                    pos["exit_reason"] = "达到 Agent 设定的最长持有期限"
        value = sum(pos["value"] for pos in self.positions)
        equity = self.cash + value
        self.peak = max(self.peak, equity)
        self.drawdown = max(self.drawdown, 1 - equity / self.peak)
        self.previous_equity = equity
        open_pnl = value - sum(pos["cost"] for pos in self.positions)
        return execution, {
            "day": day,
            "equity": round(equity, 2),
            "return_pct": round(100 * (equity / INITIAL - 1), 5),
            "realized": round(self.realized, 2),
            "open_pnl": round(open_pnl, 2),
            "open_positions": len(self.positions),
            "stale_marks": not_fresh,
            "pending_exits": sum(bool(pos["exit_reason"]) for pos in self.positions),
            "delayed_close_signal_exchanges": sorted(delayed_futures),
        }

    def open(self, day, decision, packet, prices, terms):
        if decision["action"] == "wait":
            return {"status": "wait", "reason": decision["thesis"]}
        product = decision["product"]
        if any(pos["product"] == product for pos in self.positions):
            return {"status": "blocked", "reason": "同品种已有在手仓位"}
        analysis = packet["products"][product]
        quotes = {leg["id"]: leg for leg in analysis["options"]}
        legs = decision["legs"]
        scenario = {}
        mult = analysis["multiplier"]
        if self.group == "A":
            leg = quotes[legs[0]]
            scenario["prior_stressed_target_pnl_per_lot"] = stressed_profit(
                decision["scenario_target"], leg, leg["premium"], mult
            )
        if self.group == "C":
            paired = [quotes[x] for x in legs]
            scenario["prior_close_stress"] = strangle_scenarios(
                analysis, paired, [leg["premium"] for leg in paired], decision
            )
            if not scenario["prior_close_stress"]["eligible"]:
                return {
                    "status": "blocked",
                    "reason": "前日双腿成本与20日上下界压力情景不匹配",
                    **scenario,
                }
        side = quotes[legs[0]]["side"] if len(legs) == 1 else "both"
        future = prices["future"].get(analysis["technical"]["contract"])
        if not future or not positive(future[0]):
            return {
                "status": "unfilled",
                "reason": "缺少同月份标的开盘记录",
                **scenario,
            }
        if (side == "C" and future[0] <= decision["invalidation_price"]) or (
            side == "P" and future[0] >= decision["invalidation_price"]
        ):
            return {
                "status": "blocked",
                "reason": "标的开盘已穿越 Agent 失效价",
                **scenario,
            }
        target_exit = terms["target_exit_close"]
        if target_exit is not None and (
            (side == "C" and future[0] >= target_exit)
            or (side == "P" and future[0] <= target_exit)
        ):
            return {
                "status": "blocked",
                "reason": "标的开盘已达到计划明示的止盈价",
                **scenario,
            }
        rows = [prices["option"].get(leg) for leg in legs]
        if any(not r or not positive(r[0]) or not positive(r[4]) for r in rows):
            return {
                "status": "unfilled",
                "reason": "缺少实际月份同日开盘成交代理",
                **scenario,
            }
        if self.group == "C":
            scenario["opening_stress"] = strangle_scenarios(
                analysis, paired, [r[0] for r in rows], decision
            )
            if not scenario["opening_stress"]["eligible"]:
                return {
                    "status": "blocked",
                    "reason": "开盘双腿成本超出20日上下界的压力情景",
                    **scenario,
                }
        if self.group == "A":
            edge = stressed_profit(
                decision["scenario_target"], quotes[legs[0]], rows[0][0], mult
            )
            scenario["stressed_target_pnl_per_lot"] = edge
            if edge <= 0:
                return {
                    "status": "blocked",
                    "reason": "实际开盘代理价下，目标情景已不覆盖压力成本",
                    **scenario,
                }
        if (
            terms["opening_premium_limit"] is not None
            and sum(r[0] for r in rows) > terms["opening_premium_limit"]
        ):
            return {
                "status": "blocked",
                "reason": "开盘权利金超出冻结计划明确的报价上限",
                **scenario,
            }
        opening = mult * sum(r[0] for r in rows)
        unit_cost = opening * (1 + SLIPPAGE) + FEE * len(legs)
        available_budget = min(
            self.cash,
            self.previous_equity * terms["effective_risk_bps"] / 10000,
            max(
                0, self.previous_equity * 0.08 - sum(p["cost"] for p in self.positions)
            ),
        )
        # 前一日成交量只用于保守预算。当日总量仅用于事后拒绝数量不可行的模拟。
        qty = floor(
            min(
                available_budget / unit_cost,
                min(quotes[s]["volume"] for s in legs) * 0.01,
            )
        )
        if terms["size_limit_lots"] is not None:
            qty = min(qty, terms["size_limit_lots"])
        if qty <= 0 or any(r[4] < qty for r in rows):
            return {
                "status": "unfilled",
                "reason": "风险预算或历史成交量不足以覆盖整数手",
                **scenario,
            }
        cost = unit_cost * qty
        self.opened += 1
        self.cash -= cost
        self.positions.append(
            {
                "day": day,
                "product": product,
                "legs": legs,
                "mult": mult,
                "qty": qty,
                "cost": cost,
                "underlying": analysis["technical"]["contract"],
                "value": opening * qty,
                "side": side,
                "hold": decision["holding_days"],
                "age": 0,
                "invalid": decision["invalidation_price"],
                "exit_reason": "",
                "target_exit": terms["target_exit_close"],
            }
        )
        return {
            "status": "simulated_open",
            "reason": "同日开盘价代理，缺少开盘盘口深度",
            **scenario,
            "quantity": qty,
            "underlying": analysis["technical"]["contract"],
            "underlying_open": future[0],
            "multiplier": mult,
            "raw_open_prices": [r[0] for r in rows],
            "budget_cny": round(available_budget, 2),
            "premium_and_fees": round(cost, 2),
            "legs": legs,
        }
