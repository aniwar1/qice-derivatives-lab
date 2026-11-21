"""冻结现场上下文，并验证 Agent 明示引用的证据和模拟仓位。"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import AgentStrategyPayload, EvidenceDomainSummary, SourceReference
from .research_archive import MarketObservation


def _stamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("证据或事件时间缺少时区")
    return result


def _examine_facts(result: AgentStrategyPayload, context: dict, cutoff: datetime) -> dict[str, list[dict]]:
    review = result.agent_review
    assert review is not None
    items = {source["id"]: source for source in context["sources"]["items"]}
    facts: dict[str, list[dict]] = {"fundamentals": [], "news": []}
    for dimension in facts:
        for key in getattr(review, dimension).source_ids:
            row = items.get(key)
            if (row is None or row["dimension"] != dimension or row["authority"] != "primary_official"
                or (result.product and result.product not in row["products"])
                or _stamp(row["retrieved_at"]) > cutoff
                or _stamp(row["available_at"]) > cutoff or _stamp(row["published_at"]) > cutoff):
                raise ValueError("证据未能与冻结时点、品种或研究维度对应")
            facts[dimension].append(row)
    return facts


def validate_agent_review(result: AgentStrategyPayload, context: dict,
                          observations: list[MarketObservation]) -> None:
    review = result.agent_review
    if review is None:
        raise ValueError("缺少本轮 Agent 三面复核")
    frozen = _stamp(context["cutoff"])
    if abs(result.as_of - frozen) > timedelta(seconds=1):
        raise ValueError("决策时点与冻结上下文不符")
    cutoff = min(frozen, result.as_of)
    observed = {row.artifact_sha256: row for row in observations
                if row.available_at <= cutoff and row.observed_at <= cutoff
                and (not result.product or result.product == row.product)}
    if not set(review.technical.source_ids) <= observed.keys():
        raise ValueError("技术证据未命中本轮合约与时点")
    positioning = review.positioning
    seats = {row["id"] for row in context["member_positions"]["items"]
             if row["product"] == result.product and row["contract"] == result.analysis_contract
             and row["ownership"] == "unknown" and _stamp(row["available_at"]) <= cutoff
             and _stamp(row["published_at"]) <= cutoff}
    claims_seats = positioning.classification == "exchange_member_aggregate"
    if claims_seats != bool(positioning.source_ids) or not set(positioning.source_ids) <= seats:
        raise ValueError("持仓席位归属或证据不符")
    facts = _examine_facts(result, context, cutoff)
    entries = [plan for plan in review.plans if plan.action == "paper_entry"]
    if result.decision == "futures":
        raise ValueError("本轮买方研究只支持单腿或双腿期权计划")
    directional = [plan for plan in entries if plan.strategy in ("A", "B")]
    if bool(directional) != (result.decision in ("long_call", "long_put")):
        raise ValueError("主策略与 A/B 计划不一致")
    if not entries:
        return
    quote = next((observed[key] for key in review.technical.source_ids
                  if observed[key].contract == result.analysis_contract), None)
    if (not result.product or not quote
        or quote.observed_at < cutoff - timedelta(minutes=30)
        or result.market_levels.quote_at != quote.observed_at or result.market_levels.current_price != quote.price):
        raise ValueError("模拟开仓缺少绑定实际合约的技术证据或有效标的报价")
    # 主方向投影必须与实际盘口一致；C 的独立双腿计划保存在 agent_review 中。
    books = {row["symbol"]: row for row in context["options"]["books"]}
    multiplier = context["multipliers"][result.product]
    matched_direction = False
    timely_fundamentals = [row for row in facts["fundamentals"]
                           if row.get("freshness_role") != "low_frequency_background_only"]
    for plan in entries:
        if plan.strategy == "A" and (not facts["news"] or not any(
            row.get("scope") == "direct" for row in timely_fundamentals
        )):
            raise ValueError("A 缺少该品种直接基本面或相关消息的引用")
        ticks = [books.get(symbol) for symbol in plan.legs]
        if any(tick is None or tick["contract"] != quote.contract or _stamp(tick["available_at"]) > cutoff
               or not cutoff - timedelta(minutes=2) <= _stamp(tick["observed_at"]) <= cutoff
               or plan.quantity > min(tick["bid_size"], tick["ask_size"]) for tick in ticks):
            raise ValueError("缺少模拟手数所需的同期同月买卖盘口")
        cost = sum(tick["ask"] * multiplier + context["fee_per_leg"] for tick in ticks) * plan.quantity
        budget = context["entry_budget"]
        if plan.strategy == "B" and (not timely_fundamentals or not facts["news"]):
            budget *= 0.5
        if cost > budget:
            raise ValueError("模拟组合超过入场预算")
        if plan.strategy == "A":
            tick = ticks[0]
            peers = [books.get(name) for name in plan.comparison_symbols]
            if not peers or any(peer is None or peer["contract"] != quote.contract or peer["side"] != tick["side"]
                                or peer["strike"] == tick["strike"] or _stamp(peer["available_at"]) > cutoff
                                or not cutoff - timedelta(minutes=2) <= _stamp(peer["observed_at"]) <= cutoff for peer in peers):
                raise ValueError("A 缺少同月同侧其他行权价的同期定价对照")
            target = result.market_levels.target
            if target is None or ((target - tick["strike"]) * (1 if tick["side"] == "C" else -1) - tick["ask"]) * multiplier <= context["fee_per_leg"] * 2:
                raise ValueError("A 的方向情景未覆盖期权成本与费用")
        if plan.strategy == "C":
            if result.session_daily_bar is None or result.session_daily_bar.product != result.product:
                raise ValueError("事件双买缺少绑定标的的交易时段证据")
            event = next((row for row in facts["news"] if row["id"] == plan.event_source_id), None)
            if (event is None or not event.get("scheduled_at") or not plan.event_at
                or plan.event_at != _stamp(event["scheduled_at"]) or cutoff >= plan.event_at
                or plan.event_at > cutoff + timedelta(days=plan.max_holding_days)):
                raise ValueError("事件交易缺少持有窗口内可核验的事前日程")
            stamps = [_stamp(tick["observed_at"]) for tick in ticks]
            if max(stamps) - min(stamps) > timedelta(seconds=5):
                raise ValueError("两腿盘口时间差超过五秒")
            if ({tick["side"] for tick in ticks} != {"C", "P"}
                or any((tick["strike"] - quote.price) * (1 if tick["side"] == "C" else -1) <= 0 for tick in ticks)):
                raise ValueError("宽跨式须为同月份两侧的虚值看涨及看跌")
            if plan.scenario_low is None or plan.scenario_high is None or not plan.scenario_low < quote.price < plan.scenario_high:
                raise ValueError("事件策略缺少双向波动情景")
            strikes = {tick["side"]: tick["strike"] for tick in ticks}
            intrinsic = max(plan.scenario_high - strikes["C"], strikes["P"] - plan.scenario_low, 0)
            if (intrinsic - sum(tick["ask"] for tick in ticks)) * multiplier <= 4 * context["fee_per_leg"]:
                raise ValueError("事件策略的波动情景不足以覆盖双腿权利金与费用")
        else:
            tick = ticks[0]
            option = result.option_expression
            if option is not None:
                matched_direction |= (
                    option.symbol.upper().replace("-", "") == tick["symbol"].upper().replace("-", "")
                    and option.quote_at == _stamp(tick["observed_at"])
                    and option.strike == tick["strike"]
                    and result.decision == ("long_call" if tick["side"] == "C" else "long_put")
                    and option.ask == tick["ask"] and option.bid == tick["bid"]
                )
    if directional and not matched_direction:
        raise ValueError("主策略的期权合约与冻结盘口不符")


def bind_public_domains(result: AgentStrategyPayload, context: dict,
                        observations: list[MarketObservation]) -> AgentStrategyPayload:
    """旧版展示字段由同一批已校验引用派生，避免出现第二份持仓或消息结论。"""
    review = result.agent_review
    assert review is not None
    facts = {row["id"]: row for row in context["sources"]["items"]}
    origins = [facts[key] for key in dict.fromkeys(review.fundamentals.source_ids + review.news.source_ids)]
    def summary(message: str, sources: list[SourceReference]) -> EvidenceDomainSummary:
        return EvidenceDomainSummary(status="partial" if sources else "unobservable",
                                     freshness="not_applicable", conclusion=message[:1200], sources=sources[:12])
    tech = [SourceReference(url=row.source, title=row.contract, retrieved_at=row.available_at,
                            locator=row.contract, content_sha256=row.artifact_sha256)
            for row in observations if row.artifact_sha256 in review.technical.source_ids]
    tech_domain = summary(review.technical.assessment, tech)
    if tech and all(result.as_of - row.retrieved_at <= timedelta(minutes=30) for row in tech):
        tech_domain.freshness = "fresh"
    seats = [row for row in context["member_positions"]["items"] if row["id"] in review.positioning.source_ids]
    ownership = "当前渠道缺少可核验的机构自营账户头寸披露。"
    if seats:
        ownership = f"{seats[0]['contract']} 在 {seats[0]['trade_date']} 的交易所会员席位排名可查。受益所有人及自营、代客拆分未知。"
    referenced_options = {symbol for plan in review.plans for symbol in plan.legs + plan.comparison_symbols}
    tick_refs = [SourceReference(url=tick["source_url"], title=f"{tick['symbol']} 双边盘口",
                                 locator=tick["symbol"], retrieved_at=_stamp(tick["available_at"]),
                                 content_sha256=tick["sha256"])
                 for tick in context["options"]["books"] if tick["symbol"] in referenced_options]
    option_domain = summary(result.option_market.conclusion if tick_refs else "本轮没有用于模拟建仓的有效期权盘口引用。", tick_refs)
    if tick_refs:
        option_domain.freshness = "fresh"
    return result.model_copy(update={
        "agent_review": review.model_copy(update={"positioning": review.positioning.model_copy(update={
            "assessment": ownership
        })}),
        "technical_structure": tech_domain,
        "option_market": option_domain,
        "fundamentals_news": summary(f"基本面 · {review.fundamentals.assessment}；消息面 · {review.news.assessment}", [
            SourceReference(url=row["url"], title=(row.get("title") or row["url"])[:240], retrieved_at=_stamp(row["retrieved_at"]),
                            locator=row["id"], content_sha256=row["sha256"]) for row in origins]),
        "member_flow": summary(ownership, [
            SourceReference(url=row["url"], title="上期所会员成交及持仓排名", retrieved_at=_stamp(row["available_at"]),
                            locator=row["contract"], content_sha256=row["sha256"]) for row in seats
        ]),
    })
