"""用已冻结信号比较成交时间及持仓规则。"""

from datetime import datetime, time

from .agent_replay import BJT
from .agent_replay_account import Account, SLIPPAGE


def select_order(group, decisions, packet, positions, variant):
    """盘前产生明确的有效委托，选单和持有期均不读取当日行情。"""
    primary = decisions[group]
    order = primary
    source = group
    if group == "B" and variant == "short_rotation":
        alternative = decisions["A"]
        occupied = {p["product"] for p in positions if not p["exit_reason"]}
        if (primary["action"] == "wait" or primary["product"] in occupied) and alternative["action"] == "enter" and alternative["product"] not in occupied:
            candidate = packet["products"][alternative["product"]]
            side = next(o["side"] for o in candidate["options"] if o["id"] == alternative["legs"][0])
            direction = 1 if side == "C" else -1
            trend = [candidate["technical"].get(k) for k in ("change5_pct", "close_vs_sma20_pct")]
            if all(value is not None and value * direction > 0 for value in trend):
                order = alternative
                source = "A"
    if order["action"] == "wait" or variant == "opening":
        return order, source
    if group in ("A", "B") and variant == "short_rotation":
        return order | {"holding_days": min(2, order["holding_days"])}, source
    if group == "C":
        days = packet["next_sessions"][:order["holding_days"]]
        closes = [datetime.combine(datetime.fromisoformat(day).date(), time(15), BJT) for day in days]
        opening = closes[0].replace(hour=9, minute=5)
        times = [
            datetime.fromisoformat(doc["scheduled_at"]) for doc in packet["documents"]
            if doc.get("scheduled_at") and doc["id"] in order["evidence_ids"] and order["product"] in doc["products"]
        ]
        events = [event for event in times if opening < event <= closes[-1]]
        if not events:
            raise ValueError("事件策略的盘前回执缺少持有期内的已登记事件")
        holding = next(i for i, close in enumerate(closes, 1) if close >= max(events))
        return order | {"holding_days": holding}, source
    return order, source


class ExecutionAccount(Account):
    """单腿限价仅影响入场；退出和盯市继续使用各自真实行情。"""

    def __init__(self, group, variant):
        super().__init__(group)
        self.execution_variant = variant

    def open(self, day, decision, packet, prices, terms):
        first = super().open(day, decision, packet, prices, terms)
        # 原始开盘模型先检查标的失效、止盈、证据和压力成本。只有超限价才续挂。
        if (self.execution_variant == "opening" or self.group == "C" or
                first.get("reason") != "开盘权利金超出冻结计划明确的报价上限"):
            return first
        symbol, = decision["legs"]
        row = prices["option"][symbol]
        limit = terms["opening_premium_limit"]
        # 最低成交价须为滑点留足余量。以能达到的最差限价模拟，保留成交近似说明。
        raw_price = limit / (1 + SLIPPAGE)
        if not row[2] or not 0 < row[2] < raw_price:
            return first
        option = dict(prices["option"])
        option[symbol] = [raw_price, *row[1:]]
        matched = super().open(day, decision, packet, {**prices, "option": option}, terms)
        if matched["status"] == "simulated_open":
            matched.update(
                reason="当日有效单腿限价，日低严格穿过含滑点余量的限价，按日线代理模拟成交。",
                fill_method="daily_limit_proxy",
                observed_daily_low=row[2],
                market_open_prices=[row[0]],
                raw_entry_prices=matched.pop("raw_open_prices"),
                filled_premium_including_slippage=limit,
            )
        return matched
