"""逐笔回放收盘信号与下个开盘价代理，保持权益、模型意见和成交质量可分辨。"""

from __future__ import annotations

from math import floor

from .agent_replay import positive, stressed_profit

INITIAL = 200_000.0
FIXED_TRADE_BUDGET = 10_000
FIXED_STRATEGY_VERSION = "fixed-10000-split-v2"
PER_LEG_BUDGET = FIXED_TRADE_BUDGET // 2
SETTLEMENT_REVISION = "full-order-budget-v1"
FEE = 3.0
SLIPPAGE = 0.01


def strangle_scenarios(analysis, legs, premiums, decision, quantities=None):
    low, high = analysis["technical"]["low20"], analysis["technical"]["high20"]
    if not (positive(low) and positive(high) and high > low):
        return {"eligible": False, "reason": "缺少可复核的前 20 日实盘价格区间"}
    mult = analysis["multiplier"]

    def net(target):
        return round(
            sum(
                count * stressed_profit(target, leg, premium, mult)
                for leg, premium, count in zip(
                    legs, premiums, quantities or [1] * len(legs), strict=True
                )
            ),
            4,
        )

    lower = decision["scenario_low"]
    upper = decision["scenario_high"]
    downside, upside = net(lower), net(upper)
    result = {
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

    if quantities is not None:
        result["leg_quantities"] = quantities
        result["eligible"] = all(quantities) and result["eligible"]
        result["downside_net_cny"] = result.pop("downside_net_cny_per_pair")
        result["upside_net_cny"] = result.pop("upside_net_cny_per_pair")
        result.pop("premium_cny_per_pair")
        result["premium_cny"] = round(
            sum(p * q * mult for p, q in zip(premiums, quantities, strict=True)), 4
        )
        result["roundtrip_fees_cny"] = FEE * 2 * sum(quantities)
    return result


def leg_quantities(position):
    if "leg_quantities" in position:
        return position["leg_quantities"]
    return [position["qty"]] * len(position["legs"])


def position_value(position, premiums):
    if "leg_quantities" not in position:
        return position["qty"] * position["mult"] * sum(premiums)
    return position["mult"] * sum(
        q * p for q, p in zip(position["leg_quantities"], premiums, strict=True)
    )


def closing_details(position, proceeds):
    if "leg_quantities" not in position:
        return {"quantity": position["qty"]}
    return {
        "leg_fills": [
            {
                "leg": leg,
                "quantity": qty,
                "opening_debit": round(cost, 2),
                "proceeds_after_costs": round(credit, 2),
                "pnl": round(credit - cost, 2),
            }
            for leg, qty, cost, credit in zip(
                position["legs"],
                position["leg_quantities"],
                position["leg_costs"],
                proceeds,
                strict=True,
            )
        ]
    }


def split_quantities(premiums, mult):
    return [
        floor(PER_LEG_BUDGET / (price * mult * (1 + SLIPPAGE) + FEE))
        for price in premiums
    ]


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
    terms = {
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

    if packet.get("strategy_version") in {"fixed-10000-v1", FIXED_STRATEGY_VERSION}:
        terms.pop("effective_risk_bps")
        terms.pop("agent_risk_bps")
        terms.update(
            requested_budget_cny=FIXED_TRADE_BUDGET,
            budget_reason="每笔开仓固定 10,000 元预算，按可用现金、组合敞口和整手流动性取可成交数量",
        )
    if packet.get("strategy_version") == FIXED_STRATEGY_VERSION:
        terms["budget_reason"] = (
            "按完整预算确定最大整手数量，资金、流动性或手数限制不足则整单不成交"
        )
    return terms


class Account:
    shared_leg_liquidity = False

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
        # 允许同品种多批时，同一份成交量不能给多批平仓及再开仓重复使用。
        option_open = (
            dict(prices["option"]) if self.shared_leg_liquidity else prices["option"]
        )
        for pos in list(self.positions):
            if not pos["exit_reason"]:
                continue
            rows = [option_open.get(leg) for leg in pos["legs"]]
            counts = leg_quantities(pos)
            if any(
                not row
                or not positive(row[0])
                or not positive(row[4])
                or row[4] < count
                for row, count in zip(rows, counts, strict=True)
            ):
                continue
            credits = [
                count * (row[0] * pos["mult"] * (1 - SLIPPAGE) - FEE)
                for row, count in zip(rows, counts, strict=True)
            ]
            proceeds = sum(credits)
            if "leg_quantities" not in pos:
                proceeds = pos["qty"] * (
                    sum(r[0] for r in rows) * pos["mult"] * (1 - SLIPPAGE)
                    - FEE * len(rows)
                )
            pnl = proceeds - pos["cost"]
            self.cash += proceeds
            self.realized += pnl
            self.closed += 1
            self.positions.remove(pos)
            if self.shared_leg_liquidity:
                for leg, row, count in zip(pos["legs"], rows, counts, strict=True):
                    option_open[leg] = [*row[:4], row[4] - count, *row[5:]]
            exits.append(
                {
                    "product": pos["product"],
                    "entry_day": pos["day"],
                    "legs": pos["legs"],
                    **closing_details(pos, credits),
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
        opening_prices = (
            {**prices, "option": option_open} if self.shared_leg_liquidity else prices
        )
        execution = self.open(day, decision, packet, opening_prices, terms) | terms
        execution["exits"] = exits
        not_fresh = []
        for pos in self.positions:
            option_rows = [prices["option"].get(leg) for leg in pos["legs"]]
            pos["age"] += 1
            if all(r and positive(r[3]) and positive(r[4]) for r in option_rows):
                pos["value"] = position_value(pos, [r[3] for r in option_rows])
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

    def product_entry_allowed(self, day, decision, packet):
        return all(pos["product"] != decision["product"] for pos in self.positions)

    def open(self, day, decision, packet, prices, terms):
        if decision["action"] == "wait":
            return {"status": "wait", "reason": decision["thesis"]}
        product = decision["product"]
        if not self.product_entry_allowed(day, decision, packet):
            return {"status": "blocked", "reason": "同品种已有在手仓位"}
        analysis = packet["products"][product]
        quotes = {leg["id"]: leg for leg in analysis["options"]}
        legs = decision["legs"]
        scenario = {}
        mult = analysis["multiplier"]
        requested = terms.get("requested_budget_cny")
        if requested is None:
            requested = self.previous_equity * terms["effective_risk_bps"] / 10000
        full_order = packet.get("strategy_version") == FIXED_STRATEGY_VERSION
        portfolio_cap = self.previous_equity * 0.08
        if full_order:
            portfolio_cap = max(portfolio_cap, 2 * FIXED_TRADE_BUDGET)
        available_budget = min(
            self.cash,
            requested,
            max(0, portfolio_cap - sum(p["cost"] for p in self.positions)),
        )
        split = full_order and self.group == "C"
        if self.group == "A":
            leg = quotes[legs[0]]
            scenario["prior_stressed_target_pnl_per_lot"] = stressed_profit(
                decision["scenario_target"], leg, leg["premium"], mult
            )
        if self.group == "C":
            paired = [quotes[x] for x in legs]
            prior_premiums = [leg["premium"] for leg in paired]
            prior_counts = split_quantities(prior_premiums, mult) if split else None
            if prior_counts is not None and not all(prior_counts):
                return {
                    "status": "unfilled",
                    "reason": "前收报价、逐腿预算或流动性不足以双边各买一手",
                }
            scenario["prior_close_stress"] = strangle_scenarios(
                analysis, paired, prior_premiums, decision, prior_counts
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
            premiums = [r[0] for r in rows]
            counts = split_quantities(premiums, mult) if split else None
            if counts is not None and not all(counts):
                return {
                    "status": "unfilled",
                    "reason": "开盘时至少一腿的预算或流动性不足一手，两腿均不成交",
                    **scenario,
                }
            scenario["opening_stress"] = strangle_scenarios(
                analysis, paired, premiums, decision, counts
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
        if split:
            costs = [
                (r[0] * mult * (1 + SLIPPAGE) + FEE) * q
                for r, q in zip(rows, counts, strict=True)
            ]
            sizing = {"leg_quantities": counts, "leg_costs": costs}
            quantity_details = {
                "leg_fills": [
                    {
                        "leg": leg,
                        "quantity": q,
                        "requested_budget_cny": PER_LEG_BUDGET,
                        "budget_cny": PER_LEG_BUDGET,
                        "premium_and_fees": round(cost, 2),
                    }
                    for leg, q, cost in zip(legs, counts, costs, strict=True)
                ]
            }
            cost = sum(costs)
            mark = sum(r[0] * mult * q for r, q in zip(rows, counts, strict=True))
        else:
            if full_order:
                qty = floor(FIXED_TRADE_BUDGET / unit_cost)
            else:
                qty = floor(
                    min(
                        available_budget / unit_cost,
                        min(quotes[s]["volume"] for s in legs) * 0.01,
                    )
                )
                if terms["size_limit_lots"] is not None:
                    qty = min(qty, terms["size_limit_lots"])
            counts = [qty] * len(legs)
            sizing = {"qty": qty}
            quantity_details = {"quantity": qty}
            cost = unit_cost * qty
            mark = opening * qty
        if full_order:
            limits = {
                "target_quantities": counts,
                "target_debit": round(cost, 2),
                "available_budget_cny": round(available_budget, 2),
            }
            if cost > available_budget:
                return {
                    "status": "blocked",
                    "reason": "现金或组合剩余额度不足以支付完整整手委托",
                    **limits,
                    **scenario,
                }
            if any(
                q > quotes[leg]["volume"] * 0.01
                or (
                    terms["size_limit_lots"] is not None
                    and q > terms["size_limit_lots"]
                )
                for leg, q in zip(legs, counts, strict=True)
            ):
                return {
                    "status": "unfilled",
                    "reason": "前日可参与成交量或手数上限不足以接受完整委托",
                    **limits,
                    **scenario,
                }
        # 每条腿同时满足整数手和当日成交量，才记录一笔完整模拟成交。
        if any(q <= 0 or r[4] < q for q, r in zip(counts, rows, strict=True)):
            return {
                "status": "unfilled",
                "reason": "风险预算或历史成交量不足以覆盖整数手",
                **scenario,
            }
        self.opened += 1
        self.cash -= cost
        self.positions.append(
            {
                "day": day,
                "product": product,
                "legs": legs,
                "mult": mult,
                **sizing,
                "cost": cost,
                "underlying": analysis["technical"]["contract"],
                "value": mark,
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
            **quantity_details,
            "underlying": analysis["technical"]["contract"],
            "underlying_open": future[0],
            "multiplier": mult,
            "raw_open_prices": [r[0] for r in rows],
            "budget_cny": FIXED_TRADE_BUDGET
            if full_order
            else round(available_budget, 2),
            "premium_and_fees": round(cost, 2),
            "legs": legs,
        }
