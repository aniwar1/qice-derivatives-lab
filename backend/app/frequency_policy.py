"""同一冻结信息下，由 Agent 排列可独立执行的首选和备选。"""

import re
from collections import defaultdict
from datetime import datetime
from math import floor

from .agent_replay import GROUPS, stressed_profit, wire_schema
from .agent_replay import POLICY as BASE_POLICY
from .agent_replay_account import FEE, INITIAL, SLIPPAGE

POLICY = (
    BASE_POLICY
    + """
为每组拟定有明确优先级的条件委托。primary 必填，可为 enter 或 wait。reserve 可为 null；
仅在首选 enter 且确有第二种不同品种能独立通过同样的证据、价格和风险要求时，才提交备选 enter。
每组每天最多实际开一单。真实持仓、现金与可用整手数在前一收盘的账户层逐日检查，首选被持仓或预算挡住时才检查备选。
报价字段是次开最高愿付权利金，不是必须恰等于昨收的报价；用有依据的目标、滑点、费用、期权曲面与失效风险为报价留出合理区间，
不得单纯为了成交提高上限。上下文的20万资金规模只是参考基准，风险仍是净值基点。所有可买手数是估算值。
A/B 给可复核的标的目标（B 也可明确说明剩余时间价值的假设）。说明信号变化、支持与反证、目标与整手成本之间关系。
C 仅在可信的后续事件可覆盖持有期且相应品种两尾之一可覆盖双腿压力成本时考虑；无适当证据不立项。
企业经营与市场整体证据分开；库存变化只引用同品种、同单位及同口径原文。备选不能只是同方向观点的无依据复制。
"""
)

PROTOCOL = {
    "selection": "独立模型先冻结各组主备顺序，每日每组最多一笔。前一收盘用真实账户筛选，同品种已有仓位（含待平）或整手预算不足才检查备选；开盘拒绝后不改选。资料不合规的意见直接计入风控记录。",
    "variants": {"primary": 1, "ranked": 2},
    "capital_per_sleeve": INITIAL,
    "cost": {"adverse_slippage_each_side": SLIPPAGE, "fee_per_leg_lot_side": FEE},
    "portfolio_cap_pct": 8,
    "risk_bps": {"A": 50, "B": 100, "B_missing_evidence": 50, "C": 100},
    "split": [83, 42],
    "promotion": "逐组合要求全期与后42日净收益均严格改善，开仓数增加，全期最大回撤增加不超过0.5个百分点；另与旧策略同规则比较。",
    "maturity": "沿用同一冻结主力月份期权，远月期限在独立配对实验中评价。",
    "scope": "历史研究；来源覆盖和原始可用时点沿用前次冻结档案。变化来自模型条件、成本资料与时序账户筛选。日内先后、双腿同步成交与逐笔盘口未核实。",
}


def schema(products):
    decision = wire_schema(products)["properties"]["A"]
    fields = {"primary": decision, "reserve": {"anyOf": [decision, {"type": "null"}]}}
    pair = {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {g: pair for g in GROUPS},
        "required": list(GROUPS),
        "additionalProperties": False,
    }


def signal_input(packet):
    cutoff = datetime.fromisoformat(packet["cutoff"])
    documents = []
    inventories = defaultdict(list)
    for d in packet["documents"]:
        brief = {
            k: d[k]
            for k in (
                "id",
                "products",
                "dimension",
                "scope",
                "coverage_scope",
                "freshness_role",
                "period",
                "title",
                "excerpt",
                "publisher",
                "authority",
                "available_at",
                "scheduled_at",
            )
            if k in d
        }
        brief["age_days"] = (cutoff - datetime.fromisoformat(d["available_at"])).days
        documents.append(brief)
        if d["channel"] == "czce" and "仓单数量" in d["title"]:
            m = re.search(
                r"(\d{4}-\d\d-\d\d) .*?(仓单数量[^总]*)总计\s*([\d,]+)\s*(张)",
                d["excerpt"],
            )
            if m and len(d["products"]) == 1:
                inventories[d["products"][0], m[2], m[4]].append(
                    (m[1], int(m[3].replace(",", "")), d["id"])
                )
    changes = []
    for (symbol, label, unit), rows in inventories.items():
        ordered = sorted(rows, reverse=True)
        if len(ordered) >= 2 and ordered[0][0] != ordered[1][0]:
            recent, previous = ordered[:2]
            changes.append(
                {
                    "product": symbol,
                    "series": label,
                    "unit": unit,
                    "latest_day": recent[0],
                    "previous_day": previous[0],
                    "change": recent[1] - previous[1],
                    "sources": [recent[2], previous[2]],
                }
            )
    products = {}
    for product, data in packet["products"].items():
        legs = []
        for leg in data["options"]:
            cost = data["multiplier"] * leg["premium"] * (1 + SLIPPAGE) + FEE
            tech = data["technical"]
            extreme = tech.get("high20" if leg["side"] == "C" else "low20")
            legs.append(
                leg
                | {
                    "prior_cost_cny_per_lot": round(cost, 2),
                    "lots_at_50bps": floor(1000 / cost),
                    "lots_at_100bps": floor(2000 / cost),
                    "net_at_previous_20d_extreme": stressed_profit(
                        extreme, leg, leg["premium"], data["multiplier"]
                    )
                    if extreme
                    else None,
                }
            )
        products[product] = data | {"options": legs}
    return packet | {
        "documents": documents,
        "products": products,
        "market_sources": list(packet["market_sources"]),
        "warehouse_changes": changes,
    }
