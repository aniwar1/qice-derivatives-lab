"""交易研究对消息、参与者激励及价格反应的共同要求。"""

from datetime import datetime
from math import isfinite


NEWS_REVIEW_GUIDANCE = (
    "对影响本次判断的消息，在消息面说明原始事实、首次可得时间、发布者的职责与利益关系。"
    "区分套保者、投机者及其他可能受益或承压的参与者，给出另一种合理解释。"
    "用当时已知的预期和消息前后同一实际合约的价格、成交量及持仓量反应检验是否已计价。"
    "消息尚未发布或缺少可比较的行情时，明确哪些反应仍待观察，写出会推翻判断的条件。日线只能说明当日整体反应，不能定位盘中消息触发的瞬间。"
    "发布动机或资金意图缺少直接证据时只列为假设。席位和总持仓不能识别机构自营方向。"
)


def _valid_bar(close, volume, interest):
    return (
        all(
            type(v) in (float, int) and isfinite(v) and v >= 0
            for v in (close, volume, interest)
        )
        and close > 0
    )


def live_observation_context(observation, cutoff):
    """现场研究只取截止时已取得的实际月份日线，同时保留日线质量标记。"""
    entry = observation.model_dump(mode="json", exclude={"daily_bars"})
    history = entry["reaction_history"] = []
    if observation.available_at > cutoff:
        return entry
    for row in observation.daily_bars[-5:]:
        if row.get("completed") is not True:
            continue
        day = row.get("date")
        try:
            close_at = datetime.fromisoformat(f"{day}T15:00:00+08:00")
        except ValueError:
            continue
        close, volume, interest = row.get("close"), row.get("volume"), row.get("hold")
        if close_at > cutoff or not _valid_bar(close, volume, interest):
            continue
        history.append(
            {
                "day": day,
                "contract": observation.contract,
                "close": close,
                "volume": volume,
                "open_interest": interest,
                "source_id": observation.artifact_sha256,
            }
        )
    return entry


def attach_reaction_history(packet, days, market, source_index):
    """给前向模型提供同月合约的最近五日价量仓，只引用截止前可得的原件。"""
    cutoff = datetime.fromisoformat(packet["cutoff"])
    for product, context in packet["products"].items():
        history = context["reaction_history"] = []
        if not context["technical"]:
            continue
        contract = context["technical"]["contract"]
        exchange = context["exchange"]
        for day in days[-5:]:
            source = source_index.get((day, exchange, "future"))
            row = market.get(day, {}).get("future", {}).get(contract)
            if not source or not row or len(row) < 6:
                continue
            observed = source.get("retrieved_at")
            if (
                not observed
                or datetime.fromisoformat(observed) > cutoff
                or (
                    source.get("disclosed_available_at")
                    and datetime.fromisoformat(source["disclosed_available_at"])
                    > cutoff
                )
                or source["configured_product_rows"].get(product, 0) == 0
            ):
                continue
            close, volume, interest = row[3:6]
            if not _valid_bar(close, volume, interest):
                continue
            key = f"{exchange}-future-{day}"
            packet["market_sources"][key] = {
                k: source[k]
                for k in (
                    "day",
                    "exchange",
                    "type",
                    "https",
                    "sha256",
                    "retrieved_at",
                    "disclosed_available_at",
                )
                if k in source
            }
            history.append(
                {
                    "day": day,
                    "contract": contract,
                    "close": close,
                    "volume": volume,
                    "open_interest": interest,
                    "source_id": key,
                }
            )
