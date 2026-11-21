from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.futures_main_report import (
    FuturesMainReportError,
    _assess_report_at,
    _canonical_json_file_sha256,
    _canonical_json_sha256,
    _completed_bar_hash_payload,
    _expected_market_session,
    _latest_regular_session_boundary,
    _validate_contract_for_product,
    _validate_point_plan_v3,
    _validate_raw_snapshot_binding_v3,
    _validate_scan_manifest_v3,
    _validate_tick_size_evidence_v3,
    load_policy,
)
from scripts.tick_evidence import archived_http_response_fields
from scripts.runtime_universe import allowed_products, sina_provider_symbol_for_contract
from tests.test_scan_market import replay_fixture_daily_evidence


OBSERVED_AT = "2026-09-13T12:00:00+08:00"
VALIDATED_AT = datetime.fromisoformat("2026-09-13T12:01:00+08:00")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64

EXCHANGE_DISPLAY_NAMES = {
    "DCE": "大连商品交易所",
    "SHFE": "上海期货交易所",
    "CZCE": "郑州商品交易所",
    "GFEX": "广州期货交易所",
    "INE": "上海国际能源交易中心",
}


def sina_contract_page_html(
    contract: str,
    tick_size: int | float,
    *,
    related_contracts: tuple[str, ...] = (),
) -> bytes:
    product = contract.rstrip("0123456789")
    exchange = allowed_products()[product]["exchange"]
    related = ",".join(f'"{item}"' for item in related_contracts)
    return (
        "<html><head>"
        f"<title>fixture({contract})期货行情_新浪财经</title>"
        "</head><body><script>var futures_app = {};"
        f'futures_app.code = "{contract}";'
        f"futures_app.inner_futures_per_month = {{data:[{related}]}};"
        "</script>"
        '<div class="futures-title"><h1 class="title">fixture</h1>'
        f'<span class="code">({contract})</span></div>'
        '<table id="table-futures-basic-data"><tbody>'
        f"<tr><th>最小变动价位</th><td>{tick_size}</td></tr>"
        f"<tr><th>交易代码</th><td>{product}</td>"
        f"<th>上市交易所</th><td>{EXCHANGE_DISPLAY_NAMES[exchange]}</td></tr>"
        "</tbody></table></body></html>"
    ).encode("gb18030")


def exact_tick_evidence(
    contract: str = "M2701",
    tick_size: int | float = 1,
    *,
    retrieved_at: str = "2026-09-13T11:55:00+08:00",
) -> dict:
    provider_symbol = sina_provider_symbol_for_contract(contract)
    source_url = f"https://finance.sina.com.cn/futures/quotes/{provider_symbol}.shtml"
    body = sina_contract_page_html(provider_symbol, tick_size)
    return {
        "kind": "actual_contract_http_detail",
        "contract": contract,
        "provider_symbol": provider_symbol,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        **archived_http_response_fields(
            body,
            final_url=source_url,
            status_code=200,
        ),
        "hash_scope": "http_response_body",
        "locator": "contract detail table item=最小变动价位",
        "raw_item": "最小变动价位",
        "raw_value": str(tick_size),
        "tick_size": tick_size,
        "exact": True,
    }


def source(
    url: str = "https://example.com/evidence",
    *,
    digest: str = HASH_A,
    locator: str = "table row 1",
) -> dict:
    return {
        "url": url,
        "retrieved_at": "2026-09-13T11:55:00+08:00",
        "content_sha256": digest,
        "locator": locator,
    }


def attempt(url: str = "https://www.dce.com.cn/dalianshangpin/xqsj/tjsj26/rtj/") -> dict:
    return {
        "url": url,
        "attempted_at": "2026-09-13T11:50:00+08:00",
        "locator": "M2701 member ranking",
        "result": "upstream response unavailable",
    }


def structure_artifact(as_of: str = "2026-09-10") -> dict:
    return {
        "success": True,
        "schema_version": "cn_futures_market_structure.v1",
        "algorithm_version": "causal_pivots_zones_theil_sen_tick_aligned.v1",
        "input_snapshot_sha256": HASH_B,
        "actual_main": "M2701",
        "analysis_contract": "M2701",
        "fetch_time": "2026-09-13T11:58:00+08:00",
        "as_of": as_of,
        "tick_size": 1,
        "tick_size_contract": "M2701",
        "tick_size_source": "合约规格证据",
        "tick_size_exact": True,
        "tick_size_precision": "exact_actual_contract",
        "tick_size_actionability": "exact_order_compatible",
        "tick_size_evidence": exact_tick_evidence(),
        "daily_data_evidence": {"status": "degraded"},
        "support": {"lower": 95, "upper": 98},
        "resistance": {"lower": 110, "upper": 112},
        "trend": {"direction": "up", "value": 102},
    }


def structure_evidence(as_of: str = "2026-09-10") -> dict:
    artifact = structure_artifact(as_of)
    artifact_sha = _canonical_json_file_sha256(artifact)
    binding = {
        "contract": "M2701",
        "algorithm_version": artifact["algorithm_version"],
        "as_of": as_of,
        "input_sha256": HASH_B,
        "input_snapshot_path": "evidence/raw/A.json",
        "artifact_sha256": artifact_sha,
    }
    return {
        "status": "available",
        "provenance_status": "partial_daily_data_evidence_not_verified",
        "contract": "M2701",
        "algorithm_version": artifact["algorithm_version"],
        "input_sha256": HASH_B,
        "input_snapshot_path": "evidence/raw/A.json",
        "artifact_sha256": artifact_sha,
        "artifact": artifact,
        "as_of": as_of,
        "structure_id": _canonical_json_sha256(binding),
        "direction": "bullish",
        "direction_text": "多头结构",
        "levels": {
            "support": {"lower": 95, "upper": 98, "text": "95 至 98"},
            "resistance": {"lower": 110, "upper": 112, "text": "110 至 112"},
            "trend": {"price": 102, "text": "102"},
        },
    }


def unavailable_member(name: str | None = None) -> dict:
    value = {
        "status": "unobservable",
        "conclusion": "对应官方会员榜当前不可观测，未补零。",
        "source_attempts": [attempt()],
    }
    if name is not None:
        value["name"] = name
    return value


def unavailable_member_flow() -> dict:
    domestic_names = ["中信期货", "永安期货", "国泰君安", "银河期货", "东证期货"]
    foreign_names = ["乾坤期货", "摩根大通", "瑞银期货"]
    return {
        "status": "unobservable",
        "sessions": ["2026-09-09", "2026-09-10", "2026-09-11"],
        "session_context": [],
        "top20": unavailable_member(),
        "domestic_members": {
            "status": "unobservable",
            "conclusion": "五家内资均不可观测，未补零。",
            "members": [unavailable_member(name) for name in domestic_names],
        },
        "foreign_members": {
            "status": "unobservable",
            "conclusion": "三家外资均不可观测，未补零。",
            "members": [unavailable_member(name) for name in foreign_names],
        },
    }


def structure_ref(report: dict) -> dict:
    structure = report["structure_evidence"]
    return {
        "structure_id": structure["structure_id"],
        "algorithm_version": structure["algorithm_version"],
        "as_of": structure["as_of"],
        "input_sha256": structure["input_sha256"],
    }


def base_report() -> dict:
    report = {
        "schema_version": "futures_main_report.v3",
        "action": "wait",
        "scan_scope": "single_product",
        "observed_at": OBSERVED_AT,
        "tick_size": 1,
        "target": {
            "product": "M",
            "mode": "actual_main",
            "analysis_contract": "M2701",
            "actual_main_contract": "M2701",
            "requested_contract": None,
        },
        "actual_main": {
            "status": "complete",
            "selection_basis": "highest_open_interest_then_volume_then_contract_code",
            "selected_contract": "M2701",
            "selection_claim": "verified_actual_main",
            "snapshot_status": "current_session",
            "liquidity_status": "execution_quote_incomplete",
            "chain_observed_at": "2026-09-11T23:00:00+08:00",
            "source": source(digest=HASH_B),
            "snapshot_sha256": HASH_B,
            "snapshot_path": "evidence/raw/A.json",
            "available_contract_count": 2,
            "source_record_count": 2,
            "chain_complete_for_product": True,
            "pagination_complete": True,
            "contracts": [
                {
                    "contract": "M2701",
                    "open_interest": 10000,
                    "volume": 1000,
                    "observed_at": "2026-09-11T23:00:00+08:00",
                },
                {
                    "contract": "M2703",
                    "open_interest": 5000,
                    "volume": 600,
                    "observed_at": "2026-09-11T23:00:00+08:00",
                },
            ],
        },
        "market_data": {
            "status": "fresh",
            "market_calendar": {
                "as_of": "2026-09-13T11:50:00+08:00",
                "source": source(
                    "https://www.dce.com.cn/dalianshangpin/jyrl/index.html",
                    digest=HASH_B,
                    locator="2026 trading calendar",
                ),
                "closed_dates": [],
            },
            "quote": {
                "contract": "M2701",
                "observed_at": "2026-09-11T23:00:00+08:00",
                "price": 105,
                "market_session": "closed",
                "session_closed_at": "2026-09-11T23:00:00+08:00",
                "latest_session_observation": True,
                "source": source(digest=HASH_C),
            },
            "latest_completed_bar": {
                "contract": "M2701",
                "date": "2026-09-11",
                "open": 100,
                "high": 108,
                "low": 99,
                "close": 105,
                "volume": 1000,
                "open_interest": 10000,
                "evidence_kind": "degraded_raw_completed_daily_row",
                "provenance_status": "bound_to_raw_bytes_cache_artifact_not_replayed",
                "source": source(
                    "https://www.dce.com.cn/dalianshangpin/xqsj/tjsj26/index.html",
                    digest=HASH_A,
                    locator="M2701 official daily row 2026-09-11",
                ),
            },
        },
        "structure_evidence": structure_evidence(),
        "fundamentals": {
            "status": "available",
            "gaps": [],
            "items": [
                {
                    "topic": "压榨需求与库存",
                    "direction": "bullish",
                    "as_of": "2026-09-11",
                    "as_of_precision": "date",
                    "source": source(digest=HASH_B),
                    "transmission": "库存下降通过现货升水传导至M2701。",
                    "counterevidence": "进口到港增加可能抵消库存下降。",
                    "affected_contract_months": ["2701"],
                }
            ],
        },
        "important_news": {
            "status": "search_completed_no_material_news",
            "items": [],
            "search": {
                "started_at": "2026-09-13T11:30:00+08:00",
                "completed_at": "2026-09-13T11:45:00+08:00",
                "query_terms": ["豆一 政策", "大豆 供应"],
                "materiality_criteria": "足以改变M2701供需或交易路径的新增事件。",
                "sources": [source(digest=HASH_C)],
                "result_summary": "检索完成，未发现达到重大性阈值的新事件。",
            },
        },
        "member_flow": unavailable_member_flow(),
    }
    report["point_plan"] = {
        "direction": "long",
        "setup_mode": "trend_continuation",
        "order_timing": "conditional",
        "structure_ref": structure_ref(report),
        "trigger": {
            "condition": "completed_close_at_or_above",
            "price": 106,
            "text": "完成日线收盘 >= 106",
        },
        "entry_zone": {"lower": 106, "upper": 107, "text": "106 至 107"},
        "invalidation": {
            "condition": "completed_close_at_or_below",
            "price": 94,
            "text": "完成日线收盘 <= 94",
            "thesis": "跌破冻结支撑后多头供需传导不再成立。",
        },
        "target": {
            "price": 110,
            "text": "110",
            "evidence": {
                "basis": "opposing_structure",
                "description": "目标取冻结压力区下沿。",
                "evidence_refs": ["structure_evidence.levels.resistance"],
            },
        },
        "reference_reward_risk": 3 / 13,
        "confirmation_status": "pending",
        "historical_confirmation": None,
        "valid_until_or_recalculate_condition": "新增完成K线、主力变化或证据传导改变时重算。",
    }
    report["agent_judgment"] = {
        "decision_method": "agent_discretionary_synthesis",
        "objective": "long_term_verifiable_net_profit_after_costs",
        "majority_vote_used": False,
        "rules_auto_authorized_action": False,
        "profit_guaranteed": False,
        "directional_view": "long",
        "synthesis_thesis": "库存改善提供多头驱动，但必须由完成结构和资金承担共同确认；当前保留资本。",
        "domains": {
            "technical_structure": {
                "stance": "supports_long",
                "supporting_case": "上升结构仍在，但尚未完成触发。",
                "counter_case": "当前收盘仍低于106触发价。",
                "confidence": {"value": 0.62, "rationale": "结构清楚但时点未确认。"},
                "causal_transmission": "站上触发位后趋势资金才可能扩张。",
                "evidence_refs": ["structure_evidence.artifact"],
            },
            "fundamentals_and_news": {
                "stance": "supports_long",
                "supporting_case": "库存路径提供潜在上行驱动。",
                "counter_case": "进口到港可能抵消库存收紧。",
                "confidence": {"value": 0.55, "rationale": "方向可见但强度有限。"},
                "causal_transmission": "库存下降可能推升现货升水并传导至远月。",
                "evidence_refs": ["fundamentals.items[0]", "important_news.search"],
            },
            "member_flow": {
                "stance": "unobservable",
                "supporting_case": "没有可核会员榜支持案例。",
                "counter_case": "会员资金缺失降低对主动买盘的判断力。",
                "confidence": {"value": 0.1, "rationale": "官方榜单当前不可得。"},
                "causal_transmission": "无法判断风险承担者是否已用持仓表达。",
                "evidence_refs": ["member_flow.top20.status"],
            },
        },
        "strongest_case_for": "结构与库存方向有潜在一致性。",
        "strongest_case_against": "触发未完成且会员资金不可观测。",
        "why_now": "现在需要保留观察对象，因为下一根完成K线可能形成可执行窗口。",
        "why_not_wait": "若提前配置可捕捉跳空，但当前证据不足以承担该代价。",
        "why_not_opposite": "空头缺乏结构和库存传导支持。",
        "alternative": {"action": "long", "reason": "完成106触发后重新评估多头配置。"},
        "invalidation": "结构跌破94或库存传导反转时放弃多头假设。",
        "expected_net_value": "uncertain",
        "expected_net_value_reasoning": "低点位赔率与资金缺口使成本后期望区间跨零。",
        "capital_allocation_verdict": "preserve_capital",
        "coverage_uncertainty": {
            "level": "high",
            "known_gaps": [
                "三日官方会员榜不可观测",
                "daily_data_evidence_not_verified:degraded",
            ],
            "decision_impact": "降低时点把握，选择保留资本。",
        },
        "costs_and_slippage_considered": "尚未进入订单层，按保守滑点预留后优势仍不清晰。",
        "long_term_validation_plan": "把判断、触发与自然平仓结果写入前向账本复核。",
        "competing_hypotheses": {
            "long": {"case": "结构与库存偏多。", "reason": "等待完成触发。", "disposition": "rejected"},
            "short": {"case": "触发失败可能转弱。", "reason": "缺少空头因果链。", "disposition": "rejected"},
            "wait": {"case": "等待提升信息价值。", "reason": "当前优势不足。", "disposition": "selected"},
        },
        "final_action": "wait",
        "wait_for": [
            {
                "condition": "等待下一根完成K线及缺失会员资金更新后重新比较资本机会。",
                "decision_relevance": "新证据可能让成本后净期望脱离当前跨零区间。",
                "evidence_refs": [
                    "market_data.latest_completed_bar",
                    "member_flow.status",
                ],
            }
        ],
        "reassess_by": "2026-09-13T15:00:00+08:00",
        "update_triggers": [
            {
                "trigger": "出现新完成K线、主力变化或三面因果传导更新。",
                "required_update": "重抓焦点证据并重新比较long、short、wait。",
                "evidence_refs": [
                    "market_data.latest_completed_bar",
                    "structure_evidence.status",
                ],
            }
        ],
    }
    return report


def make_directional_low_rr(report: dict) -> None:
    report["action"] = "long"
    report["actual_main"]["liquidity_status"] = "observable_trade"
    report["market_data"]["quote"].update(
        {"bid": 104, "ask": 105, "volume": 1000}
    )
    plan = report["point_plan"]
    plan["order_timing"] = "conditional"
    plan["execution_precondition"] = {
        "requires_fresh_quote": True,
        "minimum_volume": 1,
        "requires_positive_non_inverted_bid_ask": True,
        "executable_side": "ask",
    }
    plan["trigger"]["price"] = 104
    plan["trigger"]["text"] = "完成日线收盘 >= 104"
    plan["entry_zone"] = {"lower": 104, "upper": 105, "text": "104 至 105"}
    plan["reference_reward_risk"] = 5 / 11
    plan["confirmation_status"] = "confirmed"
    judgment = report["agent_judgment"]
    for field in ("wait_for", "reassess_by", "update_triggers"):
        judgment.pop(field, None)
    judgment["domains"]["technical_structure"]["stance"] = "supports_short"
    judgment["domains"]["fundamentals_and_news"]["stance"] = "supports_short"
    judgment["why_not_wait"] = "Agent判断胜率与驱动持续性足以覆盖较低点位赔率和成本。"
    judgment["why_not_opposite"] = "反向材料被判断为已计价且缺乏时点优势。"
    judgment["alternative"] = {"action": "wait", "reason": "若报价离开区间则保留资本。"}
    judgment["expected_net_value"] = "positive"
    judgment["expected_net_value_reasoning"] = "虽不足2R，但Agent主观胜率分布在扣除成本后仍给出正期望。"
    judgment["capital_allocation_verdict"] = "deploy_conditionally"
    judgment["competing_hypotheses"]["long"]["disposition"] = "selected"
    judgment["competing_hypotheses"]["wait"]["disposition"] = "rejected"
    judgment["final_action"] = "long"


def make_user_specified_main_unobservable(report: dict) -> None:
    report["target"] = {
        "product": "M",
        "mode": "user_specified_contract",
        "analysis_contract": "M2701",
        "actual_main_contract": None,
        "requested_contract": "M2701",
        "roll_context": "用户指定该月份；产品全链主力当前不可观测。",
    }
    report["actual_main"] = {
        "status": "unobservable",
        "selected_contract": None,
        "available_contract_count": 0,
        "contracts": [],
        "chain_complete_for_product": False,
        "snapshot_status": "historical_or_unbound",
        "liquidity_status": "unobservable",
        "unobservable_reason": "产品实际月份持仓链无可排名行。",
        "source_attempts": [attempt()],
    }


def bind_real_raw_and_structure(
    report: dict,
    root: Path,
    *,
    main_unobservable: bool = False,
    bid: int | None = 104,
    ask: int | None = 105,
    volume: int = 1000,
) -> Path:
    from scripts.market_structure import _compact_fetch_structure

    closes = [100, 104, 108, 103, 98, 102, 107, 101, 96, 100, 106, 102, 99, 105]
    dates = [
        "2026-08-29",
        "2026-08-30",
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
        "2026-09-05",
        "2026-09-06",
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
    ]
    rows = [
        {
            "date": day,
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 100 + index,
            "hold": 200 + index,
            "completed": True,
        }
        for index, (day, close) in enumerate(zip(dates, closes))
    ]
    chain = (
        []
        if main_unobservable
        else [
            {
                "symbol": "M2701",
                "exchange": "DCE",
                "provider_exchange": "DCE",
                "exchange_identity_status": "verified_exact",
                "open_interest": 10000,
                "volume": volume,
                "bid": bid,
                "ask": ask,
                "quote_time": "2026-09-11 23:00:00",
                "provider_quote_time": "2026-09-11 23:00:00",
                "quote_time_normalization": "provider_calendar_time_unchanged",
            },
            {
                "symbol": "M2703",
                "exchange": "DCE",
                "provider_exchange": "DCE",
                "exchange_identity_status": "verified_exact",
                "open_interest": 5000,
                "volume": 600,
                "bid": 104,
                "ask": 105,
                "quote_time": "2026-09-11 23:00:00",
                "provider_quote_time": "2026-09-11 23:00:00",
                "quote_time_normalization": "provider_calendar_time_unchanged",
            },
        ]
    )
    selection = (
        {
            "source_record_count": 0,
            "available_contract_count": 0,
            "rankable_contract_count": 0,
            "actual_main_symbol": None,
            "selection_claim": "unobservable_no_rankable_row",
            "main_liquidity_status": "unobservable",
            "chain_complete_for_product": False,
            "actual_contract_chain_sha256": _canonical_json_sha256(chain),
        }
        if main_unobservable
        else {
            "source_record_count": 2,
            "available_contract_count": 2,
            "rankable_contract_count": 2,
            "actual_main_symbol": "M2701",
            "selection_claim": "verified_actual_main",
            "main_liquidity_status": (
                "no_trade"
                if volume == 0
                else "execution_quote_incomplete"
                if bid is None or ask is None or ask < bid
                else "observable_trade"
            ),
            "chain_complete_for_product": True,
            "pagination_complete": True,
            "actual_contract_chain_sha256": _canonical_json_sha256(chain),
            "chain_time_evidence": {
                "status": "same_snapshot",
                "min_observed_at": "2026-09-11T23:00:00+08:00",
                "max_observed_at": "2026-09-11T23:00:00+08:00",
                "span_seconds": 0.0,
                "invalid_time_contracts": [],
                "future_time_contracts": [],
            },
        }
    )
    tick_evidence = exact_tick_evidence()
    raw = {
        "success": True,
        "requested_symbol": "M2701" if main_unobservable else "M0",
        "request_started_at": "2026-09-13T11:54:00+08:00",
        "chain_observed_at": "2026-09-13T11:56:00+08:00",
        "fetch_time": "2026-09-13T11:58:00+08:00",
        "quote": {"quote_time": "2026-09-11 23:00:00"},
        "actual_main": (
            None
            if main_unobservable
            else {
                "symbol": "M2701",
                "exchange": "DCE",
                "provider_exchange": "DCE",
                "exchange_identity_status": "verified_exact",
                "price": 105,
            }
        ),
        "analysis_contract": {
            "symbol": "M2701",
            "exchange": "DCE",
            "provider_exchange": "DCE",
            "exchange_identity_status": "verified_exact",
            "price": 105,
            "bid": bid,
            "ask": ask,
            "volume": volume,
            "quote_time": "2026-09-11 23:00:00",
        },
        "analysis_mode": (
            "requested_specific_contract" if main_unobservable else "product_actual_main"
        ),
        "completed_trade_date_evidence": {"date": "2026-09-11"},
        "actual_contract_chain": chain,
        "main_selection": selection,
        "technical": {
            "symbol": "M2701",
            "tick_size_contract": "M2701",
            "tick_size_source": "fixture exact contract evidence",
            "tick_size": 1,
            "tick_size_exact": True,
            "tick_size_precision": "exact_actual_contract",
            "tick_size_evidence": tick_evidence,
            "last_daily_close": 105,
            "completed_daily_rows": rows,
            "daily_rows_source": "synthetic_unit_fixture",
            "daily_completion_rule": "inclusive_explicit_completed_trade_date",
            "daily_completion_basis": "fixture_completed_trade_date_evidence",
            "daily_data_evidence": {
                "status": "degraded",
                "quality_tier": "synthetic_unit_fixture",
                "quality_equivalence": "not_equivalent_to_verified_cache",
                "source_kind": "synthetic_completed_bars",
                "scope_kind": "single_contract_unit_fixture",
                "contract": "M2701",
                "bound_completed_trade_date": "2026-09-11",
                "slice_date_range": {
                    "start": rows[0]["date"],
                    "end": rows[-1]["date"],
                },
                "slice_row_count": len(rows),
                "cache_error": "unit fixture has no published configured-product cache artifact",
            },
        },
    }
    raw_path = root / "evidence" / "raw" / "A.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    artifact = _compact_fetch_structure(raw, input_snapshot_sha256=raw_sha)
    artifact_sha = _canonical_json_file_sha256(artifact)
    direction = {"up": "bullish", "down": "bearish", "range": "sideways"}[
        artifact["trend"]["direction"]
    ]
    direction_text = {
        "bullish": "多头结构",
        "bearish": "空头结构",
        "sideways": "区间结构",
    }[direction]
    binding = {
        "contract": "M2701",
        "algorithm_version": artifact["algorithm_version"],
        "as_of": artifact["as_of"],
        "input_sha256": raw_sha,
        "input_snapshot_path": "evidence/raw/A.json",
        "artifact_sha256": artifact_sha,
    }
    report["structure_evidence"] = {
        "status": "available",
        "provenance_status": (
            "verified"
            if artifact.get("daily_data_evidence", {}).get("status") == "verified"
            else "partial_daily_data_evidence_not_verified"
        ),
        "contract": "M2701",
        "algorithm_version": artifact["algorithm_version"],
        "input_sha256": raw_sha,
        "input_snapshot_path": "evidence/raw/A.json",
        "artifact_sha256": artifact_sha,
        "artifact": artifact,
        "as_of": artifact["as_of"],
        "structure_id": _canonical_json_sha256(binding),
        "direction": direction,
        "direction_text": direction_text,
        "levels": {
            "support": {
                "lower": artifact["support"]["lower"],
                "upper": artifact["support"]["upper"],
                "text": f"{artifact['support']['lower']} 至 {artifact['support']['upper']}",
            },
            "resistance": {
                "lower": artifact["resistance"]["lower"],
                "upper": artifact["resistance"]["upper"],
                "text": f"{artifact['resistance']['lower']} 至 {artifact['resistance']['upper']}",
            },
            "trend": {
                "price": artifact["trend"]["value"],
                "text": str(artifact["trend"]["value"]),
            },
        },
    }
    if not main_unobservable:
        report["actual_main"]["snapshot_path"] = "evidence/raw/A.json"
        report["actual_main"]["snapshot_sha256"] = raw_sha
        report["actual_main"]["contracts"][0]["volume"] = volume
        report["actual_main"]["liquidity_status"] = (
            "no_trade"
            if volume == 0
            else "execution_quote_incomplete"
            if bid is None or ask is None or ask < bid
            else "observable_trade"
        )
    report["market_data"]["quote"].update(
        {
            "price": 105,
            "bid": bid,
            "ask": ask,
            "volume": volume,
            "spread": None if bid is None or ask is None else ask - bid,
            "executable_side": "ask",
            "executable_price": ask,
            "spread_cost_assumption": "按ask入场并将完整买卖价差计入保守成本。",
        }
    )
    latest = rows[-1]
    report["market_data"]["latest_completed_bar"].update(
        {
            "open": latest["open"],
            "high": latest["high"],
            "low": latest["low"],
            "close": latest["close"],
            "volume": latest["volume"],
            "open_interest": latest["hold"],
            "evidence_kind": "degraded_raw_completed_daily_row",
            "provenance_status": (
                "bound_to_raw_bytes_cache_artifact_not_replayed"
            ),
        }
    )
    plan = report["point_plan"]
    plan.update(
        {
            "order_timing": "now",
            "structure_ref": structure_ref(report),
            "trigger": {
                "condition": "completed_close_at_or_above",
                "price": 106,
                "text": "完成日线收盘 >= 106",
            },
            "entry_zone": {"lower": 104, "upper": 105, "text": "104 至 105"},
            "invalidation": {
                "condition": "completed_close_at_or_below",
                "price": 97,
                "text": "完成日线收盘 <= 97",
                "thesis": "失效后趋势与供需传导均需重估。",
            },
            "target": {
                "price": 107,
                "text": "107",
                "evidence": {
                    "basis": "opposing_structure",
                    "description": "目标取绑定压力区下沿。",
                    "evidence_refs": ["structure_evidence.levels.resistance"],
                },
            },
            "reference_reward_risk": 2 / 8,
            "confirmation_status": "pending",
            "historical_confirmation": None,
            "timing_override_rationale": "已验可成交ask仍在有限区间，Agent判断事件风险后净期望为正。",
        }
    )
    judgment = report["agent_judgment"]
    for field in ("wait_for", "reassess_by", "update_triggers"):
        judgment.pop(field, None)
    judgment["expected_net_value"] = "positive"
    judgment["expected_net_value_reasoning"] = "已绑定买卖盘与价差后，Agent判断成本后期望仍为正。"
    judgment["capital_allocation_verdict"] = "deploy_conditionally"
    judgment["why_not_wait"] = "可成交侧价格与事件窗口共同构成当下优势。"
    judgment["alternative"] = {"action": "wait", "reason": "盘口或传导改变则保留资本。"}
    judgment["competing_hypotheses"]["long"]["disposition"] = "selected"
    judgment["competing_hypotheses"]["wait"]["disposition"] = "rejected"
    judgment["final_action"] = "long"
    report["action"] = "long"
    return raw_path


def resign_bound_raw_and_structure(
    report: dict,
    raw_path: Path,
    raw: dict,
) -> None:
    """Re-sign a mutated final-focus fixture without trusting old hashes."""

    from scripts.market_structure import _compact_fetch_structure

    raw_path.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    artifact = _compact_fetch_structure(raw, input_snapshot_sha256=raw_sha)
    artifact_sha = _canonical_json_file_sha256(artifact)
    report["actual_main"]["snapshot_sha256"] = raw_sha
    structure = report["structure_evidence"]
    structure["input_sha256"] = raw_sha
    structure["artifact_sha256"] = artifact_sha
    structure["artifact"] = artifact
    binding = {
        "contract": structure["contract"],
        "algorithm_version": structure["algorithm_version"],
        "as_of": structure["as_of"],
        "input_sha256": raw_sha,
        "input_snapshot_path": structure["input_snapshot_path"],
        "artifact_sha256": artifact_sha,
    }
    structure["structure_id"] = _canonical_json_sha256(binding)
    plan = report.get("point_plan")
    if isinstance(plan, dict):
        plan["structure_ref"] = structure_ref(report)
        historical = plan.get("historical_confirmation")
        if isinstance(historical, dict):
            historical["structure_ref"] = structure_ref(report)


def bind_historical_dual_snapshots(
    report: dict,
    root: Path,
) -> tuple[Path, Path]:
    """Build S1 frozen structure evidence and an independently signed S2 raw."""

    from scripts.market_structure import _compact_fetch_structure

    final_raw_path = bind_real_raw_and_structure(report, root)
    final_raw = json.loads(final_raw_path.read_text(encoding="utf-8"))
    prehistory_dates = [
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-22",
        "2026-08-23",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
    ]
    prehistory_closes = [98, 101, 105, 102, 97, 100, 104, 99, 96, 103]
    prehistory = [
        {
            "date": day,
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 80 + index,
            "hold": 180 + index,
            "completed": True,
        }
        for index, (day, close) in enumerate(
            zip(prehistory_dates, prehistory_closes)
        )
    ]
    final_rows = [
        *prehistory,
        *final_raw["technical"]["completed_daily_rows"],
    ]
    final_raw["technical"]["completed_daily_rows"] = final_rows
    final_daily = final_raw["technical"]["daily_data_evidence"]
    final_daily["slice_date_range"]["start"] = final_rows[0]["date"]
    final_daily["slice_row_count"] = len(final_rows)
    final_raw_path.write_text(
        json.dumps(
            final_raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    report["actual_main"]["snapshot_sha256"] = hashlib.sha256(
        final_raw_path.read_bytes()
    ).hexdigest()
    frozen_raw = deepcopy(final_raw)
    frozen_rows = [
        row
        for row in frozen_raw["technical"]["completed_daily_rows"]
        if row["date"] <= "2026-09-07"
    ]
    assert frozen_rows[-1]["date"] == "2026-09-07"
    frozen_raw["request_started_at"] = "2026-09-13T11:50:00+08:00"
    frozen_raw["chain_observed_at"] = "2026-09-13T11:51:00+08:00"
    frozen_raw["fetch_time"] = "2026-09-13T11:53:00+08:00"
    frozen_raw["technical"]["tick_size_evidence"] = exact_tick_evidence(
        "M2701", 1, retrieved_at="2026-09-13T11:52:00+08:00"
    )
    frozen_raw["technical"]["completed_daily_rows"] = frozen_rows
    frozen_raw["technical"]["last_daily_close"] = frozen_rows[-1]["close"]
    frozen_raw["completed_trade_date_evidence"]["date"] = frozen_rows[-1][
        "date"
    ]
    frozen_daily = frozen_raw["technical"]["daily_data_evidence"]
    frozen_daily["bound_completed_trade_date"] = frozen_rows[-1]["date"]
    frozen_daily["slice_date_range"]["end"] = frozen_rows[-1]["date"]
    frozen_daily["slice_row_count"] = len(frozen_rows)
    frozen_path = root / "evidence" / "raw" / "A-frozen.json"
    frozen_path.write_text(
        json.dumps(
            frozen_raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    frozen_sha = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
    artifact = _compact_fetch_structure(
        frozen_raw, input_snapshot_sha256=frozen_sha
    )
    artifact_sha = _canonical_json_file_sha256(artifact)
    direction = {"up": "bullish", "down": "bearish", "range": "sideways"}[
        artifact["trend"]["direction"]
    ]
    direction_text = {
        "bullish": "多头结构",
        "bearish": "空头结构",
        "sideways": "区间结构",
    }[direction]
    relative_frozen_path = "evidence/raw/A-frozen.json"
    binding = {
        "contract": "M2701",
        "algorithm_version": artifact["algorithm_version"],
        "as_of": artifact["as_of"],
        "input_sha256": frozen_sha,
        "input_snapshot_path": relative_frozen_path,
        "artifact_sha256": artifact_sha,
    }
    report["structure_evidence"] = {
        "status": "available",
        "provenance_status": "partial_daily_data_evidence_not_verified",
        "contract": "M2701",
        "algorithm_version": artifact["algorithm_version"],
        "input_sha256": frozen_sha,
        "input_snapshot_path": relative_frozen_path,
        "artifact_sha256": artifact_sha,
        "artifact": artifact,
        "as_of": artifact["as_of"],
        "structure_id": _canonical_json_sha256(binding),
        "direction": direction,
        "direction_text": direction_text,
        "levels": {
            "support": {
                "lower": artifact["support"]["lower"],
                "upper": artifact["support"]["upper"],
                "text": (
                    f"{artifact['support']['lower']} 至 "
                    f"{artifact['support']['upper']}"
                ),
            },
            "resistance": {
                "lower": artifact["resistance"]["lower"],
                "upper": artifact["resistance"]["upper"],
                "text": (
                    f"{artifact['resistance']['lower']} 至 "
                    f"{artifact['resistance']['upper']}"
                ),
            },
            "trend": {
                "price": artifact["trend"]["value"],
                "text": str(artifact["trend"]["value"]),
            },
        },
    }
    report["point_plan"]["structure_ref"] = structure_ref(report)

    def report_bar(raw_bar: dict) -> dict:
        return {
            "contract": "M2701",
            "date": raw_bar["date"],
            "open": raw_bar["open"],
            "high": raw_bar["high"],
            "low": raw_bar["low"],
            "close": raw_bar["close"],
            "volume": raw_bar["volume"],
            "open_interest": raw_bar["hold"],
            "source": source(
                locator=f"degraded raw completed row {raw_bar['date']}"
            ),
        }

    confirmation_index = next(
        index for index, row in enumerate(final_rows) if row["date"] == "2026-09-08"
    )
    confirming = report_bar(final_rows[confirmation_index])
    subsequent = [report_bar(row) for row in final_rows[confirmation_index + 1 :]]
    normalized_chain = []
    for row in final_rows[confirmation_index:]:
        normalized_chain.append(
            {
                "contract": "M2701",
                "date": datetime.strptime(row["date"], "%Y-%m-%d").date(),
                **{
                    field: Decimal(str(row[field]))
                    for field in ("open", "high", "low", "close")
                },
                "volume": row["volume"],
                "open_interest": row["hold"],
            }
        )
    report["point_plan"]["historical_confirmation"] = {
        "structure_ref": structure_ref(report),
        "confirming_bar": confirming,
        "subsequent_bars": subsequent,
        "bar_chain_sha256": _canonical_json_sha256(
            {
                "confirming_bar": _completed_bar_hash_payload(
                    normalized_chain[0]
                ),
                "subsequent_bars": [
                    _completed_bar_hash_payload(row)
                    for row in normalized_chain[1:]
                ],
            }
        ),
    }
    report["point_plan"]["confirmation_status"] = "confirmed"
    return frozen_path, final_raw_path


def resign_frozen_structure_snapshot(
    report: dict,
    frozen_path: Path,
    frozen_raw: dict,
) -> None:
    from scripts.market_structure import _compact_fetch_structure

    frozen_path.write_text(
        json.dumps(
            frozen_raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    frozen_sha = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
    artifact = _compact_fetch_structure(
        frozen_raw, input_snapshot_sha256=frozen_sha
    )
    artifact_sha = _canonical_json_file_sha256(artifact)
    structure = report["structure_evidence"]
    structure["input_sha256"] = frozen_sha
    structure["artifact"] = artifact
    structure["artifact_sha256"] = artifact_sha
    structure["as_of"] = artifact["as_of"]
    structure["levels"] = {
        "support": {
            "lower": artifact["support"]["lower"],
            "upper": artifact["support"]["upper"],
            "text": (
                f"{artifact['support']['lower']} 至 "
                f"{artifact['support']['upper']}"
            ),
        },
        "resistance": {
            "lower": artifact["resistance"]["lower"],
            "upper": artifact["resistance"]["upper"],
            "text": (
                f"{artifact['resistance']['lower']} 至 "
                f"{artifact['resistance']['upper']}"
            ),
        },
        "trend": {
            "price": artifact["trend"]["value"],
            "text": str(artifact["trend"]["value"]),
        },
    }
    structure["direction"] = {
        "up": "bullish",
        "down": "bearish",
        "range": "sideways",
    }[artifact["trend"]["direction"]]
    structure["direction_text"] = {
        "bullish": "多头结构",
        "bearish": "空头结构",
        "sideways": "区间结构",
    }[structure["direction"]]
    structure["structure_id"] = _canonical_json_sha256(
        {
            "contract": structure["contract"],
            "algorithm_version": structure["algorithm_version"],
            "as_of": structure["as_of"],
            "input_sha256": frozen_sha,
            "input_snapshot_path": structure["input_snapshot_path"],
            "artifact_sha256": artifact_sha,
        }
    )
    report["point_plan"]["structure_ref"] = structure_ref(report)
    report["point_plan"]["historical_confirmation"]["structure_ref"] = (
        structure_ref(report)
    )


def make_available_top20(report: dict) -> None:
    official = source(
        "https://www.dce.com.cn/dalianshangpin/xqsj/tjsj26/rtj/",
        digest=HASH_B,
        locator="M2701 daily member ranking",
    )
    sessions = ["2026-09-09", "2026-09-10", "2026-09-11"]
    contexts = [
        (sessions[0], 101, 1, 900, 9800, 100),
        (sessions[1], 103, 2, 950, 9900, 100),
        (sessions[2], 105, 2, 1000, 10000, 100),
    ]
    session_context = [
        {
            "date": day,
            "contract": "M2701",
            "close": close,
            "close_change": close_change,
            "volume": volume,
            "total_open_interest": oi,
            "total_open_interest_change": oi_change,
            "price_open_interest_regime": "price_up_oi_up",
            "source": official,
        }
        for day, close, close_change, volume, oi, oi_change in contexts
    ]
    raw_rows = [
        (sessions[0], 5000, 100, 4800, 50, 9700, "both_build"),
        # Top20 membership changes, so disclosed change need not equal aggregate position difference.
        (sessions[1], 5200, 20, 4900, -10, 9800, "long_build_short_cover"),
        (sessions[2], 5300, -5, 5000, -20, 9900, "deleveraging"),
    ]
    totals = {sessions[0]: 9800, sessions[1]: 9900, sessions[2]: 10000}
    daily = []
    for day, long_oi, long_change, short_oi, short_change, previous_oi, regime in raw_rows:
        imbalance_change = long_change - short_change
        daily.append(
            {
                "date": day,
                "long_open_interest": long_oi,
                "long_change": long_change,
                "short_open_interest": short_oi,
                "short_change": short_change,
                "member_position_imbalance": long_oi - short_oi,
                "imbalance_change": imbalance_change,
                "normalization": {
                    "previous_total_open_interest": previous_oi,
                    "long_change_to_previous_oi": long_change / previous_oi,
                    "short_change_to_previous_oi": short_change / previous_oi,
                    "imbalance_change_to_previous_oi": imbalance_change / previous_oi,
                },
                "flow_regime": regime,
                "coverage": {
                    "long_to_total_oi": long_oi / totals[day],
                    "short_to_total_oi": short_oi / totals[day],
                },
            }
        )
    flow = unavailable_member_flow()
    flow.update(
        {
            "status": "partial",
            "sessions": sessions,
            "prior_session_context": {
                "date": "2026-09-08",
                "contract": "M2701",
                "close": 100,
                "total_open_interest": 9700,
                "source": official,
            },
            "session_context": session_context,
            "top20": {
                "status": "available",
                "top_n": 20,
                "composition_basis": "exchange_published_top20_components",
                "change_basis": "exchange_published_change_because_membership_can_change",
                "conclusion": "Top20变化完整，但与重点席位仍属同一证据簇。",
                "sources": [official],
                "daily": daily,
            },
        }
    )
    report["member_flow"] = flow
    member_judgment = report["agent_judgment"]["domains"]["member_flow"]
    member_judgment["stance"] = "mixed"
    member_judgment["supporting_case"] = "Top20覆盖率与变化提供部分行为证据。"
    member_judgment["counter_case"] = "重点内外资席位均不可观测。"
    member_judgment["evidence_refs"] = ["member_flow.top20.daily"]
    report["agent_judgment"]["coverage_uncertainty"]["known_gaps"] = [
        "重点会员不可观测",
        "daily_data_evidence_not_verified:degraded",
    ]


def make_all_members_available(report: dict) -> None:
    make_available_top20(report)
    flow = report["member_flow"]
    official = flow["top20"]["sources"][0]
    sessions = flow["sessions"]
    totals = {"2026-09-09": 9800, "2026-09-10": 9900, "2026-09-11": 10000}

    def member(name: str) -> dict:
        daily = []
        for index, day in enumerate(sessions):
            long_oi = 1000 + (index + 1) * 10
            short_oi = 900 + (index + 1) * 5
            long_change = 10
            short_change = 5
            previous_oi = 9700 + index * 100
            daily.append(
                {
                    "date": day,
                    "long_open_interest": long_oi,
                    "long_change": long_change,
                    "short_open_interest": short_oi,
                    "short_change": short_change,
                    "member_position_imbalance": long_oi - short_oi,
                    "imbalance_change": long_change - short_change,
                    "normalization": {
                        "previous_total_open_interest": previous_oi,
                        "long_change_to_previous_oi": long_change / previous_oi,
                        "short_change_to_previous_oi": short_change / previous_oi,
                        "imbalance_change_to_previous_oi": 5 / previous_oi,
                    },
                    "flow_regime": "both_build",
                }
            )
        return {
            "name": name,
            "status": "available",
            "conclusion": f"{name}多头增仓快于空头。",
            "sources": [official],
            "prior_positions": {
                "date": "2026-09-08",
                "long_open_interest": 1000,
                "short_open_interest": 900,
                "source": official,
            },
            "daily": daily,
        }

    for group_key in ("domestic_members", "foreign_members"):
        names = [item["name"] for item in flow[group_key]["members"]]
        flow[group_key] = {
            "status": "available",
            "conclusion": "组内重点席位均完整可观测。",
            "members": [member(name) for name in names],
        }
    flow["status"] = "available"
    domain = report["agent_judgment"]["domains"]["member_flow"]
    domain.update(
        {
            "stance": "supports_long",
            "supporting_case": "Top20与重点席位都显示多头风险承担。",
            "counter_case": "变化幅度仍需扣除换榜噪声。",
            "confidence": {"value": 0.75, "rationale": "三日完整且官方同源。"},
            "causal_transmission": "风险承担扩张可以放大上行趋势。",
            "evidence_refs": ["member_flow.top20.daily"],
        }
    )
    report["agent_judgment"]["coverage_uncertainty"] = {
        "level": "medium",
        "known_gaps": ["daily_data_evidence_not_verified:degraded"],
        "decision_impact": "证据完整，但Agent仍独立评估资本机会成本。",
    }


def make_all_market_report(report: dict) -> None:
    from scripts.scan_market import build_scan_manifest, canonical_sha256
    from tests.test_scan_market import fake_analyzer, fake_fetch

    generated_at = "2026-09-13T11:58:00+08:00"

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        manifest = build_scan_manifest(
            fetcher=fake_fetch,
            analyzer=fake_analyzer,
            evidence_dir=root / "evidence",
            manifest_dir=root,
            generated_at="2026-09-13T09:00:00+08:00",
        )
        manifest["generated_at"] = generated_at
        for row in manifest["products"]:
            row["attempted_at"] = generated_at
            if row["fetch_time"] is not None:
                row["fetch_time"] = generated_at
        focus = next(row for row in manifest["products"] if row["symbol"] == "M")
        alternative = manifest["products"][1]
        artifact = json.loads(
            (root / focus["structure_snapshot_path"]).read_text(encoding="utf-8")
        )
        artifact["tick_size_precision"] = "exact_actual_contract"
        artifact["tick_size_contract"] = focus["analysis_contract"]
        artifact["tick_size_source"] = "合约规格证据"
        focus["structure_snapshot_sha256"] = _canonical_json_file_sha256(artifact)

    report["scan_scope"] = "all_market"
    report["scan_manifest"] = manifest
    report["scan_manifest_sha256"] = canonical_sha256(manifest)
    report["focus_refresh_from_scan"] = {
        "relationship": "same_product_decision_refresh",
        "product": focus["symbol"],
        "scan_manifest_sha256": report["scan_manifest_sha256"],
        "scan_row_index": manifest["products"].index(focus),
        "scan_analysis_contract": focus["analysis_contract"],
        "scan_raw_snapshot_sha256": focus["raw_snapshot_sha256"],
        "scan_structure_snapshot_sha256": focus["structure_snapshot_sha256"],
        "scan_actual_main_liquidity_status": focus[
            "actual_main_liquidity_status"
        ],
        "refreshed_analysis_contract": report["target"]["analysis_contract"],
        "contract_transition": "unchanged",
        "transition_reason": "候选扫描后在决策时点单独刷新焦点证据。",
    }
    report["actual_main"]["snapshot_path"] = focus["raw_snapshot_path"]
    report["actual_main"]["snapshot_sha256"] = focus["raw_snapshot_sha256"]
    binding = {
        "contract": focus["analysis_contract"],
        "algorithm_version": artifact["algorithm_version"],
        "as_of": artifact["as_of"],
        "input_sha256": focus["raw_snapshot_sha256"],
        "input_snapshot_path": focus["raw_snapshot_path"],
        "artifact_sha256": focus["structure_snapshot_sha256"],
    }
    report["structure_evidence"] = {
        "status": "available",
        "provenance_status": (
            "verified"
            if artifact.get("daily_data_evidence", {}).get("status") == "verified"
            else "partial_daily_data_evidence_not_verified"
        ),
        "contract": focus["analysis_contract"],
        "algorithm_version": artifact["algorithm_version"],
        "input_sha256": focus["raw_snapshot_sha256"],
        "input_snapshot_path": focus["raw_snapshot_path"],
        "artifact_sha256": focus["structure_snapshot_sha256"],
        "artifact": artifact,
        "as_of": artifact["as_of"],
        "structure_id": _canonical_json_sha256(binding),
        "direction": "sideways",
        "direction_text": "区间结构",
        "levels": {
            "support": {
                "lower": artifact["support"]["lower"],
                "upper": artifact["support"]["upper"],
                "text": f"{artifact['support']['lower']} 至 {artifact['support']['upper']}",
            },
            "resistance": {
                "lower": artifact["resistance"]["lower"],
                "upper": artifact["resistance"]["upper"],
                "text": f"{artifact['resistance']['lower']} 至 {artifact['resistance']['upper']}",
            },
            "trend": {
                "price": artifact["trend"]["value"],
                "text": str(artifact["trend"]["value"]),
            },
        },
    }
    if artifact.get("daily_data_evidence", {}).get("status") == "verified":
        report["market_data"]["latest_completed_bar"].update(
            {
                "evidence_kind": "official_exchange_daily",
                "provenance_status": "daily_cache_artifact_replay_required",
            }
        )
    else:
        report["market_data"]["latest_completed_bar"].update(
            {
                "evidence_kind": "degraded_raw_completed_daily_row",
                "provenance_status": (
                    "bound_to_raw_bytes_cache_artifact_not_replayed"
                ),
            }
        )
    report["point_plan"] = None

    def candidate(row: dict, index: int, disposition: str | None = None) -> dict:
        value = {
            "symbol": row["symbol"],
            "contract": row["analysis_contract"],
            "scan_status": row["scan_status"],
            "actual_main_liquidity_status": row[
                "actual_main_liquidity_status"
            ],
            "raw_snapshot_sha256": row["raw_snapshot_sha256"],
            "structure_snapshot_sha256": row["structure_snapshot_sha256"],
            "evidence_refs": [
                f"scan_manifest.products[{index}].scan_status",
                f"scan_manifest.products[{index}].actual_main_liquidity_status",
                f"scan_manifest.products[{index}].raw_snapshot_sha256",
                f"scan_manifest.products[{index}].structure_snapshot_sha256",
            ],
            "causal_summary": "结构与当前驱动的因果链由Agent比较。",
            "cost_adjusted_opportunity_cost_reason": "扣除滑点和资本占用后比较相对优势。",
            "coverage_gap_impact": "本候选当前扫描无已知证据缺口。",
        }
        if disposition is not None:
            value["disposition"] = disposition
        return value

    report["candidate_comparison"] = {
        "method": "agent_comparison_without_score_or_rank_authority",
        "focus": candidate(focus, manifest["products"].index(focus)),
        "rejected_or_watch": [candidate(alternative, 1, "watch")],
        "agent_selection_reason": "焦点品种的驱动可解释性相对更强，但不由分数自动选出。",
    }


class DecisionContractV3Tests(unittest.TestCase):
    def setUp(self):
        # Decision-contract fixtures use synthetic completed bars.  Scanner
        # replay is isolated here; the production cache replay has independent
        # integration and tamper tests in test_market_data_pipeline.py.
        for target in (
            "scripts.scan_market.replay_verified_daily_evidence",
            "scripts.futures_main_report.replay_verified_daily_evidence",
        ):
            patcher = patch(target, side_effect=replay_fixture_daily_evidence)
            patcher.start()
            self.addCleanup(patcher.stop)

    def assess(self, report: dict) -> dict:
        return _assess_report_at(report, load_policy(), validated_at=VALIDATED_AT)

    def assert_rejected(self, report: dict, contains: str | None = None) -> None:
        with self.assertRaises(FuturesMainReportError) as raised:
            self.assess(report)
        if contains is not None:
            self.assertIn(contains, str(raised.exception))

    def test_canonical_file_hash_survives_decimal_report_reload(self):
        generated = {
            "price": 13530.0,
            "strength": 15.309741,
            "nested": [5.0, -38.75],
        }
        reloaded = json.loads(
            json.dumps(generated, ensure_ascii=False), parse_float=Decimal
        )
        self.assertEqual(
            _canonical_json_file_sha256(generated),
            _canonical_json_file_sha256(reloaded),
        )

    def test_valid_wait_records_agent_decision_not_trade_permission(self):
        result = self.assess(base_report())
        self.assertEqual(result["decision_status"], "agent_wait_recorded")
        self.assertFalse(result["machine_selected_action"])
        self.assertFalse(result["validation_is_trade_permission"])
        self.assertNotIn("all_hard_gates_passed", result)

    def test_sub_two_r_and_two_opposing_domains_do_not_override_agent(self):
        report = base_report()
        make_directional_low_rr(report)
        result = self.assess(report)
        self.assertEqual(result["action"], "long")
        self.assertLess(result["computed_underlying_reward_risk"], 2)
        self.assertFalse(result["majority_vote_used"])

    def test_positive_expected_value_may_still_wait_to_preserve_capital(self):
        report = base_report()
        report["agent_judgment"]["expected_net_value"] = "positive"
        report["agent_judgment"]["expected_net_value_reasoning"] = (
            "毛期望略正，但尾险和更优机会的资本占用使当前不配置更优。"
        )
        self.assertEqual(self.assess(report)["action"], "wait")

    def test_complete_supportive_evidence_and_confirmation_still_do_not_auto_trade(self):
        report = base_report()
        make_all_members_available(report)
        report["point_plan"]["trigger"]["price"] = 104
        report["point_plan"]["trigger"]["text"] = "完成日线收盘 >= 104"
        report["point_plan"]["confirmation_status"] = "confirmed"
        report["agent_judgment"]["expected_net_value"] = "positive"
        report["agent_judgment"]["expected_net_value_reasoning"] = (
            "方向期望为正，但优势薄且相对其他机会的资本占用不划算。"
        )
        result = self.assess(report)
        self.assertEqual(result["action"], "wait")
        self.assertEqual(result["evidence_status"]["member_flow"], "available")

    def test_wait_may_omit_point_plan_even_when_structure_is_available(self):
        report = base_report()
        report["point_plan"] = None
        self.assertIsNone(self.assess(report)["computed_underlying_reward_risk"])

    def test_invalid_evidence_ref_cannot_fake_a_causal_link(self):
        report = base_report()
        report["agent_judgment"]["domains"]["fundamentals_and_news"][
            "evidence_refs"
        ] = ["fundamentals.items[999]"]
        self.assert_rejected(report, "索引越界")

    def test_all_market_cannot_omit_scan_manifest(self):
        report = base_report()
        report["scan_scope"] = "all_market"
        self.assert_rejected(report, "scan_manifest")

    def test_all_market_candidate_comparison_binds_scan_rows_without_scoring(self):
        report = base_report()
        make_all_market_report(report)
        result = self.assess(report)
        self.assertEqual(result["candidate_comparison"]["alternative_count"], 1)

        missing = deepcopy(report)
        del missing["candidate_comparison"]
        self.assert_rejected(missing, "candidate_comparison")

        tampered = deepcopy(report)
        tampered["candidate_comparison"]["rejected_or_watch"][0][
            "raw_snapshot_sha256"
        ] = HASH_A
        self.assert_rejected(tampered, "候选哈希")

        scored = deepcopy(report)
        scored["candidate_comparison"]["focus"]["score"] = 99
        self.assert_rejected(scored, "score/rank")

        partial = deepcopy(report)
        configured_count = len(partial["scan_manifest"]["products"])
        row = partial["scan_manifest"]["products"][1]
        symbol = row["symbol"]
        row["scan_status"] = "partial"
        row["data_status"] = "partial"
        row["actual_main_chain_status"] = "partial"
        row["evidence_gaps"] = ["actual_main_chain_partial"]
        partial["scan_manifest"]["successful_symbols"].remove(symbol)
        partial["scan_manifest"]["partial_symbols"] = [symbol]
        partial["scan_manifest"]["successful_count"] = configured_count - 1
        partial["scan_manifest"]["partial_count"] = 1
        partial["scan_manifest"]["counts"]["data_status"] = {
            "available": configured_count - 1,
            "partial": 1,
            "unobservable": 0,
        }
        partial["scan_manifest"]["counts"]["with_evidence_gaps"] = 1
        partial["scan_manifest_sha256"] = _canonical_json_sha256(
            partial["scan_manifest"]
        )
        partial["focus_refresh_from_scan"]["scan_manifest_sha256"] = partial[
            "scan_manifest_sha256"
        ]
        alternative = partial["candidate_comparison"]["rejected_or_watch"][0]
        alternative["scan_status"] = "partial"
        alternative["coverage_gap_impact"] = "主力链不完整降低相对比较确信度。"
        alternative["evidence_refs"].append(
            "scan_manifest.products[1].evidence_gaps"
        )
        self.assertEqual(self.assess(partial)["action"], "wait")

    def test_all_market_focus_may_use_a_later_hash_bound_refresh(self):
        report = base_report()
        make_all_market_report(report)
        old_scan_hash = next(row for row in report["scan_manifest"]["products"] if row["symbol"] == "M")[
            "raw_snapshot_sha256"
        ]
        report["actual_main"]["snapshot_sha256"] = HASH_C
        structure = report["structure_evidence"]
        structure["input_sha256"] = HASH_C
        structure["artifact"]["input_snapshot_sha256"] = HASH_C
        structure["artifact_sha256"] = _canonical_json_file_sha256(
            structure["artifact"]
        )
        binding = {
            "contract": structure["contract"],
            "algorithm_version": structure["algorithm_version"],
            "as_of": structure["as_of"],
            "input_sha256": HASH_C,
            "input_snapshot_path": structure["input_snapshot_path"],
            "artifact_sha256": structure["artifact_sha256"],
        }
        structure["structure_id"] = _canonical_json_sha256(binding)

        result = self.assess(report)
        self.assertNotEqual(old_scan_hash, HASH_C)
        self.assertEqual(
            result["focus_refresh_from_scan"]["contract_transition"], "unchanged"
        )

        wrong_product = deepcopy(report)
        wrong_product["focus_refresh_from_scan"]["product"] = "JM"
        self.assert_rejected(wrong_product, "product")

        missing_link = deepcopy(report)
        del missing_link["focus_refresh_from_scan"]
        self.assert_rejected(missing_link, "focus_refresh_from_scan")

    def test_partial_scan_row_claims_are_still_bound_to_artifact_bytes(self):
        from scripts.market_structure import _compact_fetch_structure
        from scripts.scan_market import build_scan_manifest, canonical_sha256
        from tests.test_scan_market import fake_fetch

        def partial_fetch(symbol: str) -> dict:
            raw = fake_fetch(symbol)
            raw["technical"]["daily_data_evidence"] = {
                "status": "degraded",
                "quality_tier": "synthetic_partial_fixture",
            }
            return raw

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = build_scan_manifest(
                fetcher=partial_fetch,
                analyzer=_compact_fetch_structure,
                evidence_dir=root / "evidence",
                manifest_dir=root,
                generated_at="2026-09-13T09:00:00+08:00",
            )
            row = manifest["products"][1]
            symbol = row["symbol"]
            self.assertEqual(row["scan_status"], "partial")
            row["actual_main_contract"] = f"{symbol}2709"
            report = {
                "scan_scope": "all_market",
                "scan_manifest": manifest,
                "scan_manifest_sha256": canonical_sha256(manifest),
            }
            with self.assertRaisesRegex(FuturesMainReportError, "全链重算"):
                _validate_scan_manifest_v3(
                    report,
                    datetime.fromisoformat("2026-09-13T09:01:00+08:00"),
                    "M",
                    "M2701",
                    "M2701",
                    policy_sha256=hashlib.sha256(
                        (
                            Path(__file__).resolve().parents[1]
                            / "config"
                            / "futures_main_report_policy.json"
                        ).read_bytes()
                    ).hexdigest(),
                    artifact_root=root,
                )

    def test_scan_structure_levels_cannot_be_resigned_as_self_consistent_evidence(self):
        from scripts.market_structure import _compact_fetch_structure
        from scripts.scan_market import (
            build_scan_manifest,
            canonical_sha256,
            write_content_addressed_json,
        )
        from tests.test_scan_market import PRODUCT_TICKS, fake_fetch

        closes = [100, 104, 108, 103, 98, 102, 107, 101, 96, 100, 106, 102, 99, 105]
        dates = [
            "2026-08-29",
            "2026-08-30",
            "2026-08-31",
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
            "2026-09-04",
            "2026-09-05",
            "2026-09-06",
            "2026-09-07",
            "2026-09-08",
            "2026-09-09",
            "2026-09-10",
            "2026-09-11",
        ]

        def enriched_fetch(symbol: str) -> dict:
            raw = deepcopy(fake_fetch(symbol))
            tick = Decimal(str(PRODUCT_TICKS[symbol]))

            def native(value: Decimal) -> int | float:
                return int(value) if value == value.to_integral_value() else float(value)

            raw["technical"]["completed_daily_rows"] = [
                {
                    "date": day,
                    "open": native(tick * close),
                    "high": native(tick * (close + 2)),
                    "low": native(tick * (close - 2)),
                    "close": native(tick * close),
                    "volume": 100 + index,
                    "hold": 200 + index,
                    "completed": True,
                }
                for index, (day, close) in enumerate(zip(dates, closes))
            ]
            raw["technical"]["last_daily_date"] = dates[-1]
            raw["technical"]["last_daily_close"] = native(tick * closes[-1])
            return raw

        policy_sha256 = hashlib.sha256(
            (
                Path(__file__).resolve().parents[1]
                / "config"
                / "futures_main_report_policy.json"
            ).read_bytes()
        ).hexdigest()
        observed_at = datetime.fromisoformat("2026-09-13T09:01:00+08:00")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = build_scan_manifest(
                fetcher=enriched_fetch,
                analyzer=_compact_fetch_structure,
                evidence_dir=root / "evidence",
                manifest_dir=root,
                generated_at="2026-09-13T09:00:00+08:00",
            )
            focus = next(row for row in manifest["products"] if row["symbol"] == "M")
            report = {
                "scan_scope": "all_market",
                "scan_manifest": manifest,
                "scan_manifest_sha256": canonical_sha256(manifest),
            }
            baseline = _validate_scan_manifest_v3(
                report,
                observed_at,
                focus["symbol"],
                focus["analysis_contract"],
                focus["actual_main_contract"],
                policy_sha256=policy_sha256,
                artifact_root=root,
            )
            self.assertEqual(
                baseline["artifact_hashes_verified"], 2 * len(manifest["products"])
            )

            for section, field in (
                ("support", "lower"),
                ("resistance", "upper"),
                ("trend", "value"),
            ):
                with self.subTest(section=section, field=field):
                    forged_manifest = deepcopy(manifest)
                    forged_row = next(row for row in forged_manifest["products"] if row["symbol"] == "M")
                    structure_path = root / forged_row["structure_snapshot_path"]
                    forged_structure = json.loads(
                        structure_path.read_text(encoding="utf-8")
                    )
                    forged_structure[section][field] += PRODUCT_TICKS[focus["symbol"]]
                    forged_path, forged_sha = write_content_addressed_json(
                        root / "forged-structure",
                        focus["symbol"],
                        forged_structure,
                    )
                    forged_row["structure_snapshot_path"] = forged_path.relative_to(
                        root
                    ).as_posix()
                    forged_row["structure_snapshot_sha256"] = forged_sha
                    forged_report = {
                        "scan_scope": "all_market",
                        "scan_manifest": forged_manifest,
                        "scan_manifest_sha256": canonical_sha256(forged_manifest),
                    }
                    with self.assertRaisesRegex(
                        FuturesMainReportError, "确定性算法产物"
                    ):
                        _validate_scan_manifest_v3(
                            forged_report,
                            observed_at,
                            focus["symbol"],
                            focus["analysis_contract"],
                            focus["actual_main_contract"],
                            policy_sha256=policy_sha256,
                            artifact_root=root,
                        )

    def test_scan_provider_exchange_cannot_be_deleted_and_fully_re_signed(self):
        from scripts.market_structure import _compact_fetch_structure
        from scripts.scan_market import (
            build_scan_manifest,
            canonical_sha256,
            write_content_addressed_json,
        )
        from tests.test_scan_market import fake_fetch

        def current_fetch(symbol: str) -> dict:
            raw = deepcopy(fake_fetch(symbol))
            raw["request_started_at"] = "2026-09-13T11:57:58+08:00"
            raw["fetch_time"] = "2026-09-13T11:58:00+08:00"
            raw["technical"]["tick_size_evidence"]["retrieved_at"] = (
                "2026-09-13T11:57:59+08:00"
            )
            return raw

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = build_scan_manifest(
                fetcher=current_fetch,
                analyzer=_compact_fetch_structure,
                evidence_dir=root / "scan-evidence",
                manifest_dir=root,
                generated_at="2026-09-13T11:58:00+08:00",
            )
            focus = next(row for row in manifest["products"] if row["symbol"] == "M")
            raw = json.loads(
                (root / focus["raw_snapshot_path"]).read_text(encoding="utf-8")
            )
            del raw["actual_contract_chain"][0]["provider_exchange"]
            raw["actual_contract_chain"][0]["exchange_identity_status"] = (
                "provider_exchange_unobservable"
            )
            raw["main_selection"]["actual_contract_chain_sha256"] = canonical_sha256(
                raw["actual_contract_chain"]
            )
            raw_path, raw_sha = write_content_addressed_json(
                root / "forged-scan" / "raw", focus["symbol"], raw
            )
            structure = _compact_fetch_structure(
                raw, input_snapshot_sha256=raw_sha
            )
            structure_path, structure_sha = write_content_addressed_json(
                root / "forged-scan" / "structure", focus["symbol"], structure
            )
            focus.update(
                {
                    "raw_snapshot_path": raw_path.relative_to(root).as_posix(),
                    "raw_snapshot_sha256": raw_sha,
                    "structure_snapshot_path": structure_path.relative_to(root).as_posix(),
                    "structure_snapshot_sha256": structure_sha,
                    "structure_input_snapshot_sha256": raw_sha,
                    "structure_schema_version": structure["schema_version"],
                    "structure_algorithm_version": structure["algorithm_version"],
                    "structure_actual_main_contract": structure["actual_main"],
                    "structure_analysis_contract": structure["analysis_contract"],
                    "structure_tick_size": structure["tick_size"],
                    "structure_tick_size_contract": structure["tick_size_contract"],
                    "structure_tick_size_source": structure["tick_size_source"],
                    "structure_tick_size_exact": structure["tick_size_exact"],
                    "structure_tick_size_precision": structure["tick_size_precision"],
                    "structure_tick_size_actionability": structure[
                        "tick_size_actionability"
                    ],
                    "structure_tick_size_evidence": structure["tick_size_evidence"],
                }
            )

            report = base_report()
            make_all_market_report(report)
            report["scan_manifest"] = manifest
            report["scan_manifest_sha256"] = canonical_sha256(manifest)
            link = report["focus_refresh_from_scan"]
            link.update(
                {
                    "scan_manifest_sha256": report["scan_manifest_sha256"],
                    "scan_analysis_contract": focus["analysis_contract"],
                    "scan_raw_snapshot_sha256": focus["raw_snapshot_sha256"],
                    "scan_structure_snapshot_sha256": focus[
                        "structure_snapshot_sha256"
                    ],
                    "scan_actual_main_liquidity_status": focus[
                        "actual_main_liquidity_status"
                    ],
                }
            )
            report["candidate_comparison"]["focus"].update(
                {
                    "contract": focus["analysis_contract"],
                    "scan_status": focus["scan_status"],
                    "actual_main_liquidity_status": focus[
                        "actual_main_liquidity_status"
                    ],
                    "raw_snapshot_sha256": focus["raw_snapshot_sha256"],
                    "structure_snapshot_sha256": focus[
                        "structure_snapshot_sha256"
                    ],
                }
            )
            alternative = manifest["products"][1]
            report["candidate_comparison"]["rejected_or_watch"][0].update(
                {
                    "contract": alternative["analysis_contract"],
                    "scan_status": alternative["scan_status"],
                    "actual_main_liquidity_status": alternative[
                        "actual_main_liquidity_status"
                    ],
                    "raw_snapshot_sha256": alternative["raw_snapshot_sha256"],
                    "structure_snapshot_sha256": alternative[
                        "structure_snapshot_sha256"
                    ],
                }
            )

            with self.assertRaisesRegex(FuturesMainReportError, "scan_market"):
                _assess_report_at(
                    report,
                    load_policy(),
                    validated_at=VALIDATED_AT,
                    artifact_root=root,
                )

    def test_failed_scan_artifacts_cannot_be_relabelled_success(self):
        from scripts.scan_market import build_scan_manifest, canonical_sha256
        from tests.test_scan_market import fake_analyzer, fake_fetch

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            successful = build_scan_manifest(
                fetcher=fake_fetch,
                analyzer=fake_analyzer,
                evidence_dir=root / "successful",
                manifest_dir=root,
                generated_at="2026-09-13T09:00:00+08:00",
            )

            def failed_fetch(symbol: str) -> dict:
                if symbol == "M":
                    raise RuntimeError("provider offline")
                return fake_fetch(symbol)

            failed = build_scan_manifest(
                fetcher=failed_fetch,
                analyzer=fake_analyzer,
                evidence_dir=root / "failed",
                manifest_dir=root,
                generated_at="2026-09-13T09:00:00+08:00",
            )
            success_row = next(row for row in successful["products"] if row["symbol"] == "M")
            failed_row = next(row for row in failed["products"] if row["symbol"] == "M")
            # Keep every self-declared status/identity field from a valid success
            # row, but point its byte hashes at the persisted fetch/structure
            # error records.  The report validator must inspect the artifacts,
            # not trust a recomputed manifest hash.
            for field in (
                "raw_snapshot_path",
                "raw_snapshot_sha256",
                "structure_snapshot_path",
                "structure_snapshot_sha256",
                "structure_input_snapshot_sha256",
            ):
                success_row[field] = failed_row[field]
            report = {
                "scan_scope": "all_market",
                "scan_manifest": successful,
                "scan_manifest_sha256": canonical_sha256(successful),
            }
            with self.assertRaisesRegex(FuturesMainReportError, "失败raw"):
                _validate_scan_manifest_v3(
                    report,
                    datetime.fromisoformat("2026-09-13T09:01:00+08:00"),
                    "M",
                    "M2701",
                    "M2701",
                    policy_sha256=hashlib.sha256(
                        (
                            Path(__file__).resolve().parents[1]
                            / "config"
                            / "futures_main_report_policy.json"
                        ).read_bytes()
                    ).hexdigest(),
                    artifact_root=root,
                )

    def test_all_market_scan_and_later_focus_refresh_use_distinct_verified_bytes(self):
        from scripts.market_structure import _compact_fetch_structure
        from scripts.scan_market import build_scan_manifest, canonical_sha256
        from tests.test_scan_market import fake_fetch

        def run_case(*, rolled: bool) -> dict:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)

            def scan_fetch(symbol: str) -> dict:
                raw = deepcopy(fake_fetch(symbol))
                raw["request_started_at"] = "2026-09-13T11:49:58+08:00"
                raw["fetch_time"] = "2026-09-13T11:50:00+08:00"
                raw["technical"]["tick_size_evidence"]["retrieved_at"] = (
                    "2026-09-13T11:49:59+08:00"
                )
                if rolled and symbol == "M":
                    raw["actual_contract_chain"][0]["open_interest"] = 500
                    raw["actual_contract_chain"][1]["open_interest"] = 1000
                    raw["main_contract"] = {"symbol": "M2703"}
                    raw["main_selection"]["actual_main_symbol"] = "M2703"
                    raw["main_selection"]["actual_contract_chain_sha256"] = (
                        canonical_sha256(raw["actual_contract_chain"])
                    )
                    raw["technical"]["symbol"] = "M2703"
                    raw["technical"]["tick_size_contract"] = "M2703"
                    raw["technical"]["tick_size_evidence"] = exact_tick_evidence(
                        "M2703",
                        raw["technical"]["tick_size"],
                        retrieved_at="2026-09-13T11:49:59+08:00",
                    )
                return raw

            manifest = build_scan_manifest(
                fetcher=scan_fetch,
                analyzer=_compact_fetch_structure,
                evidence_dir=root / "scan-evidence",
                manifest_dir=root,
                generated_at="2026-09-13T11:50:00+08:00",
            )
            report = base_report()
            make_all_market_report(report)
            report["point_plan"] = base_report()["point_plan"]
            report["scan_manifest"] = manifest
            report["scan_manifest_sha256"] = canonical_sha256(manifest)
            focus = next(row for row in manifest["products"] if row["symbol"] == "M")
            alternative = manifest["products"][1]
            report["focus_refresh_from_scan"].update(
                {
                    "scan_manifest_sha256": report["scan_manifest_sha256"],
                    "scan_analysis_contract": focus["analysis_contract"],
                    "scan_raw_snapshot_sha256": focus["raw_snapshot_sha256"],
                    "scan_structure_snapshot_sha256": focus[
                        "structure_snapshot_sha256"
                    ],
                    "scan_actual_main_liquidity_status": focus[
                        "actual_main_liquidity_status"
                    ],
                    "contract_transition": (
                        "rolled_or_reselected" if rolled else "unchanged"
                    ),
                }
            )

            def bind_candidate(entry: dict, row: dict, index: int) -> None:
                evidence_refs = [
                    f"scan_manifest.products[{index}].scan_status",
                    f"scan_manifest.products[{index}].actual_main_liquidity_status",
                    f"scan_manifest.products[{index}].raw_snapshot_sha256",
                    f"scan_manifest.products[{index}].structure_snapshot_sha256",
                ]
                if row["evidence_gaps"]:
                    evidence_refs.append(
                        f"scan_manifest.products[{index}].evidence_gaps"
                    )
                entry.update(
                    {
                        "contract": row["analysis_contract"],
                        "scan_status": row["scan_status"],
                        "actual_main_liquidity_status": row[
                            "actual_main_liquidity_status"
                        ],
                        "raw_snapshot_sha256": row["raw_snapshot_sha256"],
                        "structure_snapshot_sha256": row[
                            "structure_snapshot_sha256"
                        ],
                        "evidence_refs": evidence_refs,
                        "coverage_gap_impact": (
                            "扫描结构有客观缺口，降低横截面比较确信度。"
                            if row["evidence_gaps"]
                            else "本候选当前扫描无已知证据缺口。"
                        ),
                    }
                )

            bind_candidate(report["candidate_comparison"]["focus"], focus, manifest["products"].index(focus))
            bind_candidate(
                report["candidate_comparison"]["rejected_or_watch"][0],
                alternative,
                1,
            )
            final_raw_path = bind_real_raw_and_structure(report, root)
            self.assertNotEqual(
                focus["raw_snapshot_sha256"], report["actual_main"]["snapshot_sha256"]
            )
            self.assertNotEqual(
                (root / focus["raw_snapshot_path"]).resolve(), final_raw_path.resolve()
            )
            return _assess_report_at(
                report,
                load_policy(),
                validated_at=VALIDATED_AT,
                artifact_root=root,
            )

        self.assertEqual(
            run_case(rolled=False)["focus_refresh_from_scan"]["contract_transition"],
            "unchanged",
        )
        self.assertEqual(
            run_case(rolled=True)["focus_refresh_from_scan"]["contract_transition"],
            "rolled_or_reselected",
        )

    def test_all_configured_products_partial_raw_chains_cannot_be_relabelled_success(
        self,
    ):
        from scripts.market_structure import _compact_fetch_structure
        from scripts.scan_market import build_scan_manifest, canonical_sha256
        from tests.test_scan_market import fake_fetch

        expected_count = len(allowed_products())

        def partial_fetch(symbol: str) -> dict:
            raw = fake_fetch(symbol)
            raw["main_selection"].update(
                {
                    "chain_complete_for_product": False,
                    "pagination_complete": False,
                    "selection_claim": "leader_of_available_rows",
                    "chain_completeness_reason": "第三方未提供官方总数或分页闭合。",
                }
            )
            return raw

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = build_scan_manifest(
                fetcher=partial_fetch,
                analyzer=_compact_fetch_structure,
                evidence_dir=root / "evidence",
                manifest_dir=root,
                generated_at="2026-09-13T09:00:00+08:00",
            )
            self.assertEqual(manifest["partial_count"], expected_count)
            for row in manifest["products"]:
                row["scan_status"] = "success"
                row["data_status"] = "available"
                row["structure_status"] = "available"
                row["actual_main_chain_status"] = "complete"
                row["evidence_gaps"] = []
                row["machine_hint"] = {
                    "review_state": "ready_for_agent_review",
                    "technical_direction": row["machine_hint"][
                        "technical_direction"
                    ],
                    "attention_reasons": [],
                }
            symbols = manifest["expected_symbols"]
            manifest["successful_symbols"] = symbols
            manifest["partial_symbols"] = []
            manifest["unobservable_symbols"] = []
            manifest["successful_count"] = expected_count
            manifest["partial_count"] = 0
            manifest["unobservable_count"] = 0
            manifest["counts"] = {
                "data_status": {
                    "available": expected_count,
                    "partial": 0,
                    "unobservable": 0,
                },
                "structure_status": {
                    "available": expected_count,
                    "partial": 0,
                    "unobservable": 0,
                },
                "with_evidence_gaps": 0,
            }
            report = {
                "scan_scope": "all_market",
                "scan_manifest": manifest,
                "scan_manifest_sha256": canonical_sha256(manifest),
            }
            with self.assertRaisesRegex(FuturesMainReportError, "scan_market"):
                _validate_scan_manifest_v3(
                    report,
                    datetime.fromisoformat("2026-09-13T09:01:00+08:00"),
                    "M",
                    "M2701",
                    "M2701",
                    policy_sha256=hashlib.sha256(
                        (
                            Path(__file__).resolve().parents[1]
                            / "config"
                            / "futures_main_report_policy.json"
                        ).read_bytes()
                    ).hexdigest(),
                    artifact_root=root,
                )

    def test_actual_main_available_count_is_recomputed(self):
        report = base_report()
        report["actual_main"]["available_contract_count"] = 1
        self.assert_rejected(report, "available_contract_count")

    def test_partial_main_allows_provisional_claim_and_non_tied_null_volume(self):
        report = base_report()
        main = report["actual_main"]
        main.update(
            {
                "status": "partial",
                "selection_claim": "provisional_leader_of_observed_oi_rows",
                "chain_complete_for_product": False,
                "pagination_complete": False,
                "source_record_count": 3,
                "incomplete_reason": "另一实际月份OI不可观测。",
                "selection_uncertainty": ["unknown_open_interest_contract"],
            }
        )
        main["contracts"][0]["volume"] = None
        main["liquidity_status"] = "unobservable"
        result = self.assess(report)
        self.assertEqual(result["evidence_status"]["actual_main_chain"], "partial")

    def test_bare_previously_confirmed_on_is_rejected(self):
        report = base_report()
        report["point_plan"]["previously_confirmed_on"] = "2026-09-09"
        report["point_plan"]["confirmation_status"] = "confirmed"
        self.assert_rejected(report, "previously_confirmed_on")

    def test_trigger_and_invalidation_cannot_overlap(self):
        report = base_report()
        report["point_plan"]["trigger"]["price"] = 94
        report["point_plan"]["trigger"]["text"] = "完成日线收盘 >= 94"
        self.assert_rejected(report, "严格分离")

    def test_order_timing_now_requires_fresh_market(self):
        report = base_report()
        make_directional_low_rr(report)
        report["point_plan"]["order_timing"] = "now"
        report["market_data"]["status"] = "stale"
        report["market_data"]["market_calendar"]["as_of"] = (
            "2026-09-11T11:50:00+08:00"
        )
        report["actual_main"]["snapshot_status"] = "historical_or_unbound"
        report["actual_main"]["selection_claim"] = "complete_historical_chain_leader"
        self.assert_rejected(report, "order_timing=now")

    def test_stale_main_chain_cannot_claim_current_verified_main(self):
        report = base_report()
        report["actual_main"]["chain_observed_at"] = "2026-09-10T23:00:00+08:00"
        for item in report["actual_main"]["contracts"]:
            item["observed_at"] = "2026-09-10T23:00:00+08:00"
        self.assert_rejected(report, "snapshot_status")

    def test_structure_numbers_cannot_be_changed_and_self_rehashed(self):
        report = base_report()
        report["structure_evidence"]["levels"]["support"] = {
            "lower": 90,
            "upper": 93,
            "text": "90 至 93",
        }
        binding = {
            "contract": "M2701",
            "algorithm_version": report["structure_evidence"]["algorithm_version"],
            "as_of": report["structure_evidence"]["as_of"],
            "input_sha256": HASH_B,
            "artifact_sha256": report["structure_evidence"]["artifact_sha256"],
        }
        report["structure_evidence"]["structure_id"] = _canonical_json_sha256(binding)
        report["point_plan"]["structure_ref"] = structure_ref(report)
        self.assert_rejected(report, "artifact")

    def test_structure_display_text_must_match_numbers(self):
        report = base_report()
        report["structure_evidence"]["levels"]["trend"]["text"] = "103"
        self.assert_rejected(report, "文本")

    def test_source_requires_content_hash(self):
        report = base_report()
        del report["fundamentals"]["items"][0]["source"]["content_sha256"]
        self.assert_rejected(report, "content_sha256")

    def test_cli_path_mode_recomputes_raw_file_bytes(self):
        report = base_report()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw_path = root / "evidence" / "raw" / "A.json"
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(FuturesMainReportError, "字节SHA-256"):
                _assess_report_at(
                    report,
                    load_policy(),
                    validated_at=VALIDATED_AT,
                    artifact_root=root,
                )

    def test_raw_exact_tick_claim_requires_contract_and_source(self):
        raw = {
            "success": True,
            "request_started_at": "2026-09-13T11:54:00+08:00",
            "chain_observed_at": "2026-09-13T11:56:00+08:00",
            "fetch_time": "2026-09-13T11:58:00+08:00",
            "actual_main": {"symbol": "M2701"},
            "analysis_contract": {"symbol": "M2701"},
            "technical": {
                "tick_size": 1,
                "tick_size_exact": True,
                "tick_size_precision": "exact_actual_contract",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "evidence" / "raw" / "A.json"
            path.parent.mkdir(parents=True)

            def invoke(payload: dict) -> None:
                path.write_text(
                    json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                _validate_raw_snapshot_binding_v3(
                    {"observed_at": OBSERVED_AT},
                    analysis_contract="M2701",
                    selected_actual_main_contract="M2701",
                    tick_size=1,
                    main_summary={
                        "status": "partial",
                        "snapshot_path": "evidence/raw/A.json",
                        "snapshot_sha256": digest,
                    },
                    structure_summary={
                        "status": "available",
                        "input_snapshot_path": "evidence/raw/A.json",
                        "input_sha256": digest,
                        "tick_size_exact": True,
                    },
                    scan_summary=None,
                    artifact_root=root,
                )

            with self.assertRaisesRegex(FuturesMainReportError, "tick_size_contract"):
                invoke(raw)
            raw["technical"]["tick_size_contract"] = "M2701"
            with self.assertRaisesRegex(FuturesMainReportError, "tick_size_source"):
                invoke(raw)

    def test_tick_evidence_validates_source_value_and_fetch_window(self):
        valid = exact_tick_evidence()
        self.assertEqual(
            _validate_tick_size_evidence_v3(
                valid,
                contract="M2701",
                tick_size=1,
                exact=True,
                not_before=datetime.fromisoformat("2026-09-13T11:54:00+08:00"),
                observed_at=datetime.fromisoformat("2026-09-13T11:58:00+08:00"),
                label="tick",
            ),
            valid,
        )
        for mutation, expected in (
            ({"source_url": "https://example.com/M2701"}, "新浪实际合约详情页"),
            ({"raw_value": "2"}, "重算tick_size"),
            ({"retrieved_at": "2026-09-13T11:53:59+08:00"}, "不得早于"),
        ):
            with self.subTest(mutation=mutation):
                evidence = exact_tick_evidence()
                evidence.update(mutation)
                with self.assertRaisesRegex(FuturesMainReportError, expected):
                    _validate_tick_size_evidence_v3(
                        evidence,
                        contract="M2701",
                        tick_size=1,
                        exact=True,
                        not_before=datetime.fromisoformat(
                            "2026-09-13T11:54:00+08:00"
                        ),
                        observed_at=datetime.fromisoformat(
                            "2026-09-13T11:58:00+08:00"
                        ),
                        label="tick",
                    )

    def test_tick_evidence_replays_archived_http_bytes_not_a_claimed_hash(self):
        valid = exact_tick_evidence()
        mutations = (
            {"content_sha256": "0" * 64},
            {"response_content_length": valid["response_content_length"] + 1},
            {"response_final_url": "https://finance.sina.com.cn/futures/quotes/M2703.shtml"},
            {"response_status_code": 206},
            {"response_body_gzip_base64": "not-base64"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                evidence = exact_tick_evidence()
                evidence.update(mutation)
                with self.assertRaisesRegex(FuturesMainReportError, "HTTP原文归档"):
                    _validate_tick_size_evidence_v3(
                        evidence,
                        contract="M2701",
                        tick_size=1,
                        exact=True,
                        not_before=datetime.fromisoformat(
                            "2026-09-13T11:54:00+08:00"
                        ),
                        observed_at=datetime.fromisoformat(
                            "2026-09-13T11:58:00+08:00"
                        ),
                        label="tick",
                    )

        source_url = "https://finance.sina.com.cn/futures/quotes/M2701.shtml"
        for body in (
            sina_contract_page_html("M2701", 2),
            sina_contract_page_html("M2703", 1, related_contracts=("M2701",)),
        ):
            evidence = exact_tick_evidence()
            evidence.update(
                archived_http_response_fields(
                    body,
                    final_url=source_url,
                    status_code=200,
                )
            )
            with self.assertRaisesRegex(FuturesMainReportError, "HTTP原文归档"):
                _validate_tick_size_evidence_v3(
                    evidence,
                    contract="M2701",
                    tick_size=1,
                    exact=True,
                    not_before=datetime.fromisoformat(
                        "2026-09-13T11:54:00+08:00"
                    ),
                    observed_at=datetime.fromisoformat(
                        "2026-09-13T11:58:00+08:00"
                    ),
                    label="tick",
                )

    def test_tick_archive_rejects_resigned_wrong_month_page_with_related_target(self):
        source_url = "https://finance.sina.com.cn/futures/quotes/CU2610.shtml"
        wrong_page = sina_contract_page_html(
            "CU2611", 10, related_contracts=("CU2610",)
        )
        self.assertIn("CU2610", wrong_page.decode("gb18030"))
        evidence = exact_tick_evidence("CU2610", 10)
        evidence.update(
            archived_http_response_fields(
                wrong_page,
                final_url=source_url,
                status_code=200,
            )
        )
        with self.assertRaisesRegex(FuturesMainReportError, "HTTP原文归档"):
            _validate_tick_size_evidence_v3(
                evidence,
                contract="CU2610",
                tick_size=10,
                exact=True,
                not_before=datetime.fromisoformat("2026-09-13T11:54:00+08:00"),
                observed_at=datetime.fromisoformat("2026-09-13T11:58:00+08:00"),
                label="tick",
            )

    def test_tick_archive_binds_basic_table_product_and_exchange(self):
        source_url = "https://finance.sina.com.cn/futures/quotes/M2701.shtml"
        valid_body = sina_contract_page_html("M2701", 1).decode("gb18030")
        for body_text in (
            valid_body.replace("<td>M</td>", "<td>JM</td>", 1),
            valid_body.replace("大连商品交易所", "上海期货交易所", 1),
        ):
            with self.subTest(body=body_text):
                evidence = exact_tick_evidence()
                evidence.update(
                    archived_http_response_fields(
                        body_text.encode("gb18030"),
                        final_url=source_url,
                        status_code=200,
                    )
                )
                with self.assertRaisesRegex(FuturesMainReportError, "HTTP原文归档"):
                    _validate_tick_size_evidence_v3(
                        evidence,
                        contract="M2701",
                        tick_size=1,
                        exact=True,
                        not_before=datetime.fromisoformat(
                            "2026-09-13T11:54:00+08:00"
                        ),
                        observed_at=datetime.fromisoformat(
                            "2026-09-13T11:58:00+08:00"
                        ),
                        label="tick",
                    )

    def test_contract_codes_use_canonical_delivery_months(self):
        self.assertEqual(
            _validate_contract_for_product("CF2701", "CF", "CZCE", "contract"),
            "CF2701",
        )
        for contract, product, exchange in (
            ("CF701", "CF", "CZCE"),
            ("M8888", "M", "DCE"),
            ("M9999", "M", "DCE"),
            ("M2713", "M", "DCE"),
        ):
            with self.subTest(contract=contract), self.assertRaises(
                FuturesMainReportError
            ):
                _validate_contract_for_product(contract, product, exchange, "contract")

    def test_contract_lifecycle_is_bound_to_each_fact_observation_date(self):
        reference_date = datetime.fromisoformat("2026-09-13T12:00:00+08:00").date()
        for contract in ("CU2609", "CU3009"):
            with self.subTest(valid_boundary=contract):
                self.assertEqual(
                    _validate_contract_for_product(
                        contract,
                        "CU",
                        "SHFE",
                        "contract",
                        reference_date=reference_date,
                    ),
                    contract,
                )
        for contract in ("CU2608", "CU3010", "CU9912"):
            with self.subTest(outside_lifecycle=contract), self.assertRaisesRegex(
                FuturesMainReportError, r"0\.\.48 month horizon"
            ):
                _validate_contract_for_product(
                    contract,
                    "CU",
                    "SHFE",
                    "contract",
                    reference_date=reference_date,
                )

        # A newly signed report cannot make a syntactically valid last-century
        # month current: target validation uses the report observation date
        # before trusting any downstream raw/manifest claims.
        report = base_report()
        report["target"].update(
            {
                "product": "CU",
                "analysis_contract": "CU9912",
                "actual_main_contract": "CU9912",
            }
        )
        self.assert_rejected(report, "0..48 month horizon")

        stale_chain = base_report()
        stale_chain["actual_main"]["contracts"][1]["contract"] = "M9912"
        self.assert_rejected(stale_chain, "0..48 month horizon")

        signed_scan = base_report()
        make_all_market_report(signed_scan)
        pb_row = next(
            row
            for row in signed_scan["scan_manifest"]["products"]
            if row["symbol"] == "CU"
        )
        pb_row["declared_actual_main_contract"] = "CU9912"
        signed_scan["scan_manifest_sha256"] = _canonical_json_sha256(
            signed_scan["scan_manifest"]
        )
        signed_scan["focus_refresh_from_scan"]["scan_manifest_sha256"] = signed_scan[
            "scan_manifest_sha256"
        ]
        self.assert_rejected(signed_scan, "0..48 month horizon")

    def test_contract_product_prefixes_cannot_collide(self):
        reference_date = datetime.fromisoformat("2026-09-13T12:00:00+08:00").date()
        for contract, product, exchange in (
            ("M2701", "M", "DCE"),
            ("CU2701", "CU", "SHFE"),
            ("RU2701", "RU", "SHFE"),
            ("AP2701", "AP", "CZCE"),
            ("CU2701", "CU", "SHFE"),
        ):
            with self.subTest(valid_contract=contract):
                self.assertEqual(
                    _validate_contract_for_product(
                        contract,
                        product,
                        exchange,
                        "contract",
                        reference_date=reference_date,
                    ),
                    contract,
                )
        for contract, claimed_product, claimed_exchange in (
            ("CU2701", "M", "DCE"),
            ("RU2701", "M", "DCE"),
            ("AP2701", "M", "DCE"),
            ("CU2701", "M", "DCE"),
        ):
            with self.subTest(contract=contract), self.assertRaisesRegex(
                FuturesMainReportError, "不是"
            ):
                _validate_contract_for_product(
                    contract,
                    claimed_product,
                    claimed_exchange,
                    "contract",
                    reference_date=reference_date,
                )

    def test_raw_request_mode_exchange_and_batch_identity_are_bound(self):
        base_raw = {
            "success": True,
            "requested_symbol": "M0",
            "request_started_at": "2026-09-13T11:54:00+08:00",
            "chain_observed_at": "2026-09-13T11:56:00+08:00",
            "fetch_time": "2026-09-13T11:58:00+08:00",
            "actual_main": {
                "symbol": "M2701",
                "exchange": "DCE",
                "provider_exchange": "dce",
                "exchange_identity_status": "verified_exact",
            },
            "analysis_contract": {
                "symbol": "M2701",
                "exchange": "DCE",
                "provider_exchange": "dce",
                "exchange_identity_status": "verified_exact",
            },
            "analysis_mode": "product_actual_main",
            "actual_contract_chain": [
                {
                    "symbol": "M2701",
                    "exchange": "DCE",
                    "provider_exchange": "dce",
                    "exchange_identity_status": "verified_exact",
                }
            ],
        }
        report = {
            "observed_at": OBSERVED_AT,
            "target": {
                "product": "M",
                "mode": "actual_main",
                "analysis_contract": "M2701",
                "actual_main_contract": "M2701",
                "requested_contract": None,
            },
        }

        def invoke(payload: dict) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / "raw.json"
                path.write_text(
                    json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                _validate_raw_snapshot_binding_v3(
                    report,
                    analysis_contract="M2701",
                    selected_actual_main_contract="M2701",
                    tick_size=1,
                    main_summary={
                        "status": "partial",
                        "snapshot_path": "raw.json",
                        "snapshot_sha256": digest,
                    },
                    structure_summary={"status": "unobservable"},
                    scan_summary=None,
                    artifact_root=root,
                    product="M",
                    exchange="DCE",
                )

        wrong_request = deepcopy(base_raw)
        wrong_request["requested_symbol"] = "M2701"
        with self.assertRaisesRegex(FuturesMainReportError, "品种入口请求"):
            invoke(wrong_request)

        wrong_mode = deepcopy(base_raw)
        wrong_mode["analysis_mode"] = "requested_specific_contract"
        with self.assertRaisesRegex(FuturesMainReportError, "品种入口请求"):
            invoke(wrong_mode)

        wrong_exchange = deepcopy(base_raw)
        wrong_exchange["analysis_contract"]["provider_exchange"] = "SHFE"
        with self.assertRaisesRegex(FuturesMainReportError, "provider_exchange"):
            invoke(wrong_exchange)

        missing_provider = deepcopy(base_raw)
        del missing_provider["analysis_contract"]["provider_exchange"]
        missing_provider["analysis_contract"]["exchange_identity_status"] = (
            "provider_exchange_unobservable"
        )
        with self.assertRaisesRegex(FuturesMainReportError, "provider_exchange"):
            invoke(missing_provider)

        forged_persisted_status = deepcopy(base_raw)
        forged_persisted_status["actual_contract_chain"][0][
            "exchange_identity_status"
        ] = "verified_named_alias"
        with self.assertRaisesRegex(FuturesMainReportError, "exchange_identity_status"):
            invoke(forged_persisted_status)

        missing_configured_exchange = deepcopy(base_raw)
        del missing_configured_exchange["actual_contract_chain"][0]["exchange"]
        with self.assertRaisesRegex(FuturesMainReportError, "exchange.*精确等于"):
            invoke(missing_configured_exchange)

        wrong_time = deepcopy(base_raw)
        wrong_time["chain_observed_at"] = "2026-09-13T11:53:59+08:00"
        with self.assertRaisesRegex(FuturesMainReportError, "顺序不闭合"):
            invoke(wrong_time)

        from tests.test_scan_market import fake_fetch

        expired_chain = deepcopy(fake_fetch("M"))
        expired_chain.update(
            {
                "request_started_at": "2026-09-13T11:54:00+08:00",
                "chain_observed_at": "2026-09-13T11:56:00+08:00",
                "fetch_time": "2026-09-13T11:58:00+08:00",
                "requested_symbol": "M0",
                "analysis_mode": "product_actual_main",
            }
        )
        expired_chain["technical"]["tick_size_evidence"]["retrieved_at"] = (
            "2026-09-13T11:55:00+08:00"
        )
        expired_chain["actual_main"] = deepcopy(
            expired_chain["actual_contract_chain"][0]
        )
        expired_chain["analysis_contract"] = deepcopy(expired_chain["actual_main"])
        expired_chain["actual_contract_chain"][1]["symbol"] = "M9912"
        with self.assertRaisesRegex(FuturesMainReportError, r"0\.\.48 month horizon"):
            invoke(expired_chain)

    def test_user_specified_structure_raw_is_verified_even_when_main_is_unobservable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = base_report()
            make_user_specified_main_unobservable(report)
            raw_path = bind_real_raw_and_structure(
                report, root, main_unobservable=True
            )
            report["action"] = "wait"
            report["point_plan"] = None
            judgment = report["agent_judgment"]
            judgment["expected_net_value"] = "uncertain"
            judgment["expected_net_value_reasoning"] = (
                "主力身份不可观测时不部署资本，但保留方向假设。"
            )
            judgment["capital_allocation_verdict"] = "preserve_capital"
            judgment["alternative"] = {
                "action": "long",
                "reason": "主力身份与可成交性恢复后再评估配置。",
            }
            judgment["competing_hypotheses"]["long"]["disposition"] = "rejected"
            judgment["competing_hypotheses"]["wait"]["disposition"] = "selected"
            judgment["final_action"] = "wait"
            wait_template = base_report()["agent_judgment"]
            for field in ("wait_for", "reassess_by", "update_triggers"):
                judgment[field] = deepcopy(wait_template[field])
            result = _assess_report_at(
                report,
                load_policy(),
                validated_at=VALIDATED_AT,
                artifact_root=root,
            )
            self.assertTrue(result["raw_provenance_bytes_verified"])
            self.assertEqual(result["action"], "wait")

            raw_path.write_text(
                raw_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
            )
            with self.assertRaisesRegex(FuturesMainReportError, "字节SHA-256"):
                _assess_report_at(
                    report,
                    load_policy(),
                    validated_at=VALIDATED_AT,
                    artifact_root=root,
                )

            raw_path.unlink()
            with self.assertRaisesRegex(FuturesMainReportError, "不存在"):
                _assess_report_at(
                    report,
                    load_policy(),
                    validated_at=VALIDATED_AT,
                    artifact_root=root,
                )

    def test_final_focus_provider_exchange_cannot_be_deleted_and_re_signed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = base_report()
            raw_path = bind_real_raw_and_structure(report, root)
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            for value in (
                raw["actual_contract_chain"][0],
                raw["actual_main"],
                raw["analysis_contract"],
            ):
                del value["provider_exchange"]
                value["exchange_identity_status"] = (
                    "provider_exchange_unobservable"
                )
            raw["main_selection"]["actual_contract_chain_sha256"] = (
                _canonical_json_sha256(raw["actual_contract_chain"])
            )
            resign_bound_raw_and_structure(report, raw_path, raw)

            with self.assertRaisesRegex(FuturesMainReportError, "provider_exchange"):
                _assess_report_at(
                    report,
                    load_policy(),
                    validated_at=VALIDATED_AT,
                    artifact_root=root,
                )

    def test_final_focus_replays_chain_hash_time_normalization_and_complete_claim(self):
        def assert_resigned_rejected(mutator, expected: str) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                report = base_report()
                raw_path = bind_real_raw_and_structure(report, root)
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                mutator(raw)
                resign_bound_raw_and_structure(report, raw_path, raw)
                with self.assertRaisesRegex(FuturesMainReportError, expected):
                    _assess_report_at(
                        report,
                        load_policy(),
                        validated_at=VALIDATED_AT,
                        artifact_root=root,
                    )

        def forge_chain_hash(raw: dict) -> None:
            raw["main_selection"]["actual_contract_chain_sha256"] = HASH_A

        def forge_chain_time(raw: dict) -> None:
            raw["main_selection"]["chain_time_evidence"]["min_observed_at"] = (
                "2026-09-11T22:59:00+08:00"
            )

        def forge_complete_claim(raw: dict) -> None:
            raw["main_selection"]["selection_claim"] = "leader_of_available_rows"
            raw["main_selection"]["chain_completeness_reason"] = (
                "伪造为只有可得行。"
            )

        def forge_weekend_normalization(raw: dict) -> None:
            raw["quote"]["quote_time"] = "2026-09-12 21:00:00"
            for row in raw["actual_contract_chain"]:
                row["provider_quote_time"] = "2026-09-14 21:00:00"
                row["quote_time"] = "2026-09-12 21:00:00"
                row["quote_time_normalization"] = (
                    "derived_calendar_time_from_direct_quote_for_next_trade_day_night_session"
                )
            raw["main_selection"]["actual_contract_chain_sha256"] = (
                _canonical_json_sha256(raw["actual_contract_chain"])
            )
            raw["main_selection"]["chain_time_evidence"] = {
                "status": "same_snapshot",
                "min_observed_at": "2026-09-12T21:00:00+08:00",
                "max_observed_at": "2026-09-12T21:00:00+08:00",
                "span_seconds": 0.0,
                "invalid_time_contracts": [],
                "future_time_contracts": [],
            }

        for name, mutator, expected in (
            ("chain_hash", forge_chain_hash, "chain_sha256_conflict"),
            ("chain_time", forge_chain_time, "time_evidence_conflict"),
            ("complete_claim", forge_complete_claim, "actual_main.status"),
            ("weekend_normalization", forge_weekend_normalization, "time_normalization"),
        ):
            with self.subTest(name=name):
                assert_resigned_rejected(mutator, expected)

    def test_now_binds_verified_price_time_book_and_report_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = base_report()
            bind_real_raw_and_structure(report, root)
            boundary = datetime.fromisoformat("2026-09-13T12:05:00+08:00")
            result = _assess_report_at(
                report,
                load_policy(),
                validated_at=boundary,
                artifact_root=root,
            )
            self.assertTrue(result["raw_provenance_bytes_verified"])

            stale = deepcopy(report)
            with self.assertRaisesRegex(FuturesMainReportError, "live_report_max_age"):
                _assess_report_at(
                    stale,
                    load_policy(),
                    validated_at=datetime.fromisoformat(
                        "2026-09-13T12:05:01+08:00"
                    ),
                    artifact_root=root,
                )

            wrong_price = deepcopy(report)
            wrong_price["market_data"]["quote"]["price"] = 104
            with self.assertRaisesRegex(FuturesMainReportError, "报告价格"):
                _assess_report_at(
                    wrong_price,
                    load_policy(),
                    validated_at=VALIDATED_AT,
                    artifact_root=root,
                )

    def test_latest_completed_bar_must_equal_verified_final_raw_last_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = base_report()
            bind_real_raw_and_structure(report, root)
            report["market_data"]["latest_completed_bar"]["close"] = 106
            report["market_data"]["latest_completed_bar"]["high"] = 109
            report["point_plan"]["confirmation_status"] = "confirmed"
            with self.assertRaisesRegex(
                FuturesMainReportError, "逐字段等于已验final raw"
            ):
                _assess_report_at(
                    report,
                    load_policy(),
                    validated_at=VALIDATED_AT,
                    artifact_root=root,
                )

    def test_official_daily_bar_requires_exchange_official_source(self):
        report = base_report()
        report["market_data"]["latest_completed_bar"].update(
            {
                "evidence_kind": "official_exchange_daily",
                "provenance_status": "daily_cache_artifact_replay_required",
            }
        )
        report["structure_evidence"]["artifact"]["daily_data_evidence"] = {
            "status": "verified"
        }
        report["structure_evidence"]["provenance_status"] = "verified"
        report["structure_evidence"]["artifact_sha256"] = (
            _canonical_json_file_sha256(
                report["structure_evidence"]["artifact"]
            )
        )
        binding = {
            key: report["structure_evidence"][key]
            for key in (
                "contract",
                "algorithm_version",
                "as_of",
                "input_sha256",
                "input_snapshot_path",
                "artifact_sha256",
            )
        }
        report["structure_evidence"]["structure_id"] = _canonical_json_sha256(
            binding
        )
        report["point_plan"]["structure_ref"] = structure_ref(report)
        report["market_data"]["latest_completed_bar"]["source"] = source(
            "https://example.com/forged-daily"
        )
        self.assert_rejected(report, "DCE 官方域名")

    def test_holiday_previous_day_cannot_open_early_morning_night_session(self):
        observed_at = datetime.fromisoformat("2026-09-15T00:30:00+08:00")
        closed_dates = {datetime.strptime("2026-09-14", "%Y-%m-%d").date()}
        policy = load_policy()
        for product in ("CU", "CU"):
            with self.subTest(product=product):
                self.assertEqual(
                    _expected_market_session(
                        observed_at, product, policy, closed_dates
                    ),
                    "closed",
                )
                self.assertEqual(
                    _latest_regular_session_boundary(
                        observed_at, product, policy, closed_dates
                    ),
                    datetime.fromisoformat("2026-09-11T15:00:00+08:00"),
                )

    def test_now_rejects_zero_volume_missing_or_inverted_book(self):
        for kwargs, expected in (
            ({"volume": 0}, "volume>0"),
            ({"bid": None}, "只能使用conditional"),
            ({"ask": None}, "只能使用conditional"),
            ({"bid": 106, "ask": 105}, "只能使用conditional"),
        ):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                report = base_report()
                bind_real_raw_and_structure(report, root, **kwargs)
                with self.assertRaisesRegex(FuturesMainReportError, expected):
                    _assess_report_at(
                        report,
                        load_policy(),
                        validated_at=VALIDATED_AT,
                        artifact_root=root,
                    )

    def test_highest_oi_zero_volume_stays_main_but_is_not_now_executable(self):
        report = base_report()
        report["actual_main"]["contracts"][0]["volume"] = 0
        report["actual_main"]["liquidity_status"] = "no_trade"
        result = self.assess(report)
        self.assertEqual(result["actual_main_contract"], "M2701")
        self.assertEqual(
            result["evidence_status"]["actual_main_liquidity"], "no_trade"
        )

    def test_directional_capital_action_rejects_zero_volume_analysis_contract(self):
        report = base_report()
        make_directional_low_rr(report)
        report["actual_main"]["contracts"][0]["volume"] = 0
        report["actual_main"]["liquidity_status"] = "no_trade"
        report["market_data"]["quote"]["volume"] = 0
        self.assert_rejected(report, "volume>0")

    def test_user_specified_contract_uses_its_own_execution_facts(self):
        report = base_report()
        make_directional_low_rr(report)
        make_user_specified_main_unobservable(report)
        result = self.assess(report)
        self.assertEqual(result["action"], "long")
        self.assertEqual(result["actual_main_contract"], None)

    def test_incomplete_book_keeps_conditional_agent_action_with_refresh_precondition(self):
        report = base_report()
        make_directional_low_rr(report)
        report["actual_main"]["liquidity_status"] = "execution_quote_incomplete"
        report["market_data"]["quote"]["ask"] = None
        self.assert_rejected(report, "timing_override_rationale")

        report["point_plan"]["timing_override_rationale"] = (
            "当前有正成交量但卖一缺失，仅保留条件方向；执行前重抓可成交盘口。"
        )
        report["point_plan"]["execution_precondition"] = {
            "requires_fresh_quote": True,
            "minimum_volume": 1,
            "requires_positive_non_inverted_bid_ask": True,
            "executable_side": "ask",
        }
        self.assertEqual(self.assess(report)["action"], "long")

    def test_every_conditional_directional_action_requires_execution_precondition(self):
        report = base_report()
        make_directional_low_rr(report)
        del report["point_plan"]["execution_precondition"]
        self.assert_rejected(report, "execution_precondition")

    def test_agent_judgment_rejects_hidden_vote_score_and_gate_results(self):
        for field, value in (
            ("majority_vote_result", "2-to-1"),
            ("weighted_score", 99),
            ("all_rules_passed", True),
            ("hard_gate_result", "pass"),
        ):
            with self.subTest(field=field):
                report = base_report()
                report["agent_judgment"][field] = value
                self.assert_rejected(report, field)

    def test_v3_top_level_rejects_decision_authority_and_unknown_fields(self):
        for field, value in (
            ("weighted_score", 99),
            ("candidate_rank", 1),
            ("majority_vote_result", "2-to-1"),
            ("hard_gate_result", "pass"),
            ("validation_is_trade_permission", True),
            ("machine_selected_action", True),
            ("unreviewed_extension", "value"),
        ):
            with self.subTest(field=field):
                report = base_report()
                report[field] = value
                self.assert_rejected(report, field)

    def test_wait_requires_structured_reassessment_conditions(self):
        for field in ("wait_for", "reassess_by", "update_triggers"):
            with self.subTest(field=field):
                report = base_report()
                del report["agent_judgment"][field]
                self.assert_rejected(report, field)

        forged_ref = base_report()
        forged_ref["agent_judgment"]["wait_for"][0]["evidence_refs"] = [
            "market_data.items[999]"
        ]
        self.assert_rejected(forged_ref, "不存在的报告字段")

        for deadline in (OBSERVED_AT, "2026-09-13T11:59:59+08:00"):
            with self.subTest(deadline=deadline):
                non_future = base_report()
                non_future["agent_judgment"]["reassess_by"] = deadline
                self.assert_rejected(non_future, "必须晚于报告观察时间")

    def test_directional_action_view_divergence_requires_structured_rationale(self):
        report = base_report()
        make_directional_low_rr(report)
        report["agent_judgment"]["directional_view"] = "short"
        self.assert_rejected(report, "view_action_divergence")

        report["agent_judgment"]["view_action_divergence"] = {
            "directional_view": "short",
            "final_action": "long",
            "rationale": "中期观点偏空，但已验证的短时事件窗口使小额多头路径期望为正。",
            "capital_allocation_logic": "只部署事件窗口风险预算，失效即一次全平。",
            "evidence_refs": [
                "structure_evidence.artifact",
                "important_news.search",
            ],
        }
        self.assertEqual(self.assess(report)["action"], "long")

    def test_degraded_daily_evidence_requires_explicit_agent_coverage_gap(self):
        report = base_report()
        report["agent_judgment"]["coverage_uncertainty"]["known_gaps"] = [
            "三日官方会员榜不可观测"
        ]
        self.assert_rejected(report, "daily_data_evidence_not_verified")

        directional = base_report()
        make_directional_low_rr(directional)
        result = self.assess(directional)
        self.assertEqual(result["action"], "long")
        self.assertEqual(
            result["evidence_status"]["technical_structure_provenance"],
            "partial_daily_data_evidence_not_verified",
        )

    def test_no_material_news_cannot_hide_placeholder_event(self):
        report = base_report()
        report["important_news"]["headline"] = "无"
        self.assert_rejected(report, "伪事件")

    def test_agent_must_compare_opposite_and_all_three_hypotheses(self):
        report = base_report()
        del report["agent_judgment"]["why_not_opposite"]
        self.assert_rejected(report, "why_not_opposite")
        report = base_report()
        del report["agent_judgment"]["competing_hypotheses"]["short"]
        self.assert_rejected(report, "long、short、wait")

    def test_unobservable_structure_wait_needs_no_fabricated_levels(self):
        report = base_report()
        report["structure_evidence"] = {
            "status": "unobservable",
            "provenance_status": "unobservable",
            "direction": "unobservable",
            "levels": None,
            "unobservable_reason": "完成K线不足，不能稳定识别结构。",
            "source_attempts": [attempt("https://example.com/structure")],
        }
        report["point_plan"] = None
        domain = report["agent_judgment"]["domains"]["technical_structure"]
        domain["stance"] = "unobservable"
        domain["evidence_refs"] = ["structure_evidence.status"]
        report["agent_judgment"]["coverage_uncertainty"]["known_gaps"].append(
            "结构不可观测"
        )
        result = self.assess(report)
        self.assertEqual(result["directional_view"], "long")
        self.assertIsNone(result["computed_underlying_reward_risk"])
        self.assertEqual(result["evidence_status"]["technical_structure"], "unobservable")

    def test_historical_confirmation_requires_bound_complete_bar_chain(self):
        report = base_report()
        report["structure_evidence"] = structure_evidence("2026-09-08")
        report["point_plan"]["structure_ref"] = structure_ref(report)
        confirm = {
            "contract": "M2701",
            "date": "2026-09-09",
            "open": 104,
            "high": 108,
            "low": 103,
            "close": 107,
            "volume": 900,
            "open_interest": 9800,
            "source": source(digest=HASH_A),
        }
        day_two = {
            "contract": "M2701",
            "date": "2026-09-10",
            "open": 107,
            "high": 108,
            "low": 104,
            "close": 105,
            "volume": 950,
            "open_interest": 9900,
            "source": source(digest=HASH_B),
        }
        latest = deepcopy(report["market_data"]["latest_completed_bar"])
        latest.pop("evidence_kind")
        verified_rows = []
        for bar in (confirm, day_two, latest):
            verified_rows.append(
                {
                    "contract": "M2701",
                    "date": datetime.strptime(bar["date"], "%Y-%m-%d").date(),
                    **{
                        field: Decimal(str(bar[field]))
                        for field in ("open", "high", "low", "close")
                    },
                    "volume": bar["volume"],
                    "open_interest": bar["open_interest"],
                }
            )
        chain = {
            "confirming_bar": confirm,
            "subsequent_bars": [day_two, latest],
        }
        replayed_chain_payload = {
            "confirming_bar": _completed_bar_hash_payload(verified_rows[0]),
            "subsequent_bars": [
                _completed_bar_hash_payload(bar) for bar in verified_rows[1:]
            ],
        }
        report["point_plan"]["historical_confirmation"] = {
            "structure_ref": structure_ref(report),
            **chain,
            "bar_chain_sha256": _canonical_json_sha256(replayed_chain_payload),
        }
        report["point_plan"]["confirmation_status"] = "confirmed"
        self.assert_rejected(report, "独立冻结structure snapshot")

        structure_summary = {
            "status": "available",
            "structure_id": report["structure_evidence"]["structure_id"],
            "algorithm_version": report["structure_evidence"]["algorithm_version"],
            "as_of": datetime.strptime("2026-09-08", "%Y-%m-%d").date(),
            "input_sha256": report["structure_evidence"]["input_sha256"],
            "tick_size_exact": True,
            "tick_size_actionability": "exact_order_compatible",
        }
        market_summary = {
            "status": "fresh",
            "latest_bar": verified_rows[-1],
            "closed_dates": set(),
            "quote_price": Decimal("105"),
        }
        self.assertIsNotNone(
            _validate_point_plan_v3(
                report,
                datetime.fromisoformat(OBSERVED_AT),
                "wait",
                "M2701",
                Decimal("1"),
                structure_summary,
                market_summary,
                verified_completed_daily_rows=verified_rows,
                require_verified_bar_binding=True,
            )
        )
        tampered = deepcopy(report)
        tampered["point_plan"]["historical_confirmation"]["subsequent_bars"] = [latest]
        tampered["point_plan"]["historical_confirmation"]["bar_chain_sha256"] = (
            _canonical_json_sha256(
                {
                    "confirming_bar": _completed_bar_hash_payload(verified_rows[0]),
                    "subsequent_bars": [
                        _completed_bar_hash_payload(verified_rows[-1])
                    ],
                }
            )
        )
        with self.assertRaisesRegex(FuturesMainReportError, "连续覆盖"):
            _validate_point_plan_v3(
                tampered,
                datetime.fromisoformat(OBSERVED_AT),
                "wait",
                "M2701",
                Decimal("1"),
                structure_summary,
                market_summary,
                verified_completed_daily_rows=verified_rows,
                require_verified_bar_binding=True,
            )

        fabricated = deepcopy(report)
        fabricated_confirming = fabricated["point_plan"]["historical_confirmation"][
            "confirming_bar"
        ]
        fabricated_confirming["close"] = 108
        fabricated_confirming["high"] = 109
        forged_payload = deepcopy(replayed_chain_payload)
        forged_payload["confirming_bar"]["close"] = 108
        forged_payload["confirming_bar"]["high"] = 109
        fabricated["point_plan"]["historical_confirmation"]["bar_chain_sha256"] = (
            _canonical_json_sha256(forged_payload)
        )
        with self.assertRaisesRegex(FuturesMainReportError, "逐字段等于已验final raw"):
            _validate_point_plan_v3(
                fabricated,
                datetime.fromisoformat(OBSERVED_AT),
                "wait",
                "M2701",
                Decimal("1"),
                structure_summary,
                market_summary,
                verified_completed_daily_rows=verified_rows,
                require_verified_bar_binding=True,
            )

    def test_historical_confirmation_replays_frozen_and_final_raw_snapshots(self):
        def frozen_daily_anchor(frozen_path: Path) -> dict:
            frozen_raw = json.loads(frozen_path.read_text(encoding="utf-8"))
            return deepcopy(
                {
                    "completed_daily_rows": frozen_raw["technical"][
                        "completed_daily_rows"
                    ],
                    "daily_data_evidence": frozen_raw["technical"][
                        "daily_data_evidence"
                    ],
                }
            )

        def assess_with_independent_anchor(
            value: dict, root: Path, anchor: dict
        ) -> dict:
            def replay(technical: dict, *, contract: str) -> dict:
                observed = {
                    "completed_daily_rows": technical.get("completed_daily_rows"),
                    "daily_data_evidence": technical.get("daily_data_evidence"),
                }
                if observed != anchor:
                    raise ValueError("frozen S1 differs from independent daily anchor")
                return dict(technical["daily_data_evidence"])

            with patch(
                "scripts.futures_main_report.replay_daily_artifact_evidence",
                side_effect=replay,
            ):
                return _assess_report_at(
                    value,
                    load_policy(),
                    validated_at=VALIDATED_AT,
                    artifact_root=root,
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = base_report()
            frozen_path, _ = bind_historical_dual_snapshots(report, root)
            result = assess_with_independent_anchor(
                report, root, frozen_daily_anchor(frozen_path)
            )
            self.assertTrue(result["completed_daily_bar_bound_to_raw_bytes"])
            self.assertFalse(result["daily_cache_artifact_replay_verified"])
            self.assertFalse(result["upstream_response_identity_verified"])
            self.assertTrue(result["frozen_structure_snapshot_bytes_verified"])
            self.assertTrue(frozen_path.is_file())

        def build_case(root: Path) -> tuple[dict, Path, Path, dict]:
            value = base_report()
            frozen, final = bind_historical_dual_snapshots(value, root)
            return value, frozen, final, frozen_daily_anchor(frozen)

        with self.subTest("tampered_frozen_overlap"), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, frozen_path, _, anchor = build_case(root)
            frozen_raw = json.loads(frozen_path.read_text(encoding="utf-8"))
            row = next(
                item
                for item in frozen_raw["technical"]["completed_daily_rows"]
                if item["date"] == "2026-09-07"
            )
            row["close"] += 1
            frozen_raw["technical"]["last_daily_close"] = row["close"]
            resign_frozen_structure_snapshot(report, frozen_path, frozen_raw)
            with self.assertRaisesRegex(
                FuturesMainReportError, "冻结S1必须独立重放已锚定日线artifact"
            ):
                assess_with_independent_anchor(report, root, anchor)

        with self.subTest("missing_frozen_overlap_row"), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, frozen_path, _, anchor = build_case(root)
            frozen_raw = json.loads(frozen_path.read_text(encoding="utf-8"))
            frozen_raw["technical"]["completed_daily_rows"] = [
                item
                for item in frozen_raw["technical"]["completed_daily_rows"]
                if item["date"] != "2026-09-04"
            ]
            frozen_raw["technical"]["daily_data_evidence"]["slice_row_count"] -= 1
            resign_frozen_structure_snapshot(report, frozen_path, frozen_raw)
            with self.assertRaisesRegex(
                FuturesMainReportError, "冻结S1必须独立重放已锚定日线artifact"
            ):
                assess_with_independent_anchor(report, root, anchor)

        with self.subTest("tampered_final_overlap"), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, _, final_path, anchor = build_case(root)
            final_raw = json.loads(final_path.read_text(encoding="utf-8"))
            row = next(
                item
                for item in final_raw["technical"]["completed_daily_rows"]
                if item["date"] == "2026-09-07"
            )
            row["close"] += 1
            final_path.write_text(
                json.dumps(
                    final_raw,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            report["actual_main"]["snapshot_sha256"] = hashlib.sha256(
                final_path.read_bytes()
            ).hexdigest()
            with self.assertRaisesRegex(
                FuturesMainReportError, "重叠完成K线必须逐字段全等"
            ):
                assess_with_independent_anchor(report, root, anchor)

        with self.subTest("missing_final_confirmation_row"), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, _, final_path, anchor = build_case(root)
            final_raw = json.loads(final_path.read_text(encoding="utf-8"))
            final_raw["technical"]["completed_daily_rows"] = [
                item
                for item in final_raw["technical"]["completed_daily_rows"]
                if item["date"] != "2026-09-10"
            ]
            final_raw["technical"]["daily_data_evidence"]["slice_row_count"] -= 1
            final_path.write_text(
                json.dumps(
                    final_raw,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            report["actual_main"]["snapshot_sha256"] = hashlib.sha256(
                final_path.read_bytes()
            ).hexdigest()
            with self.assertRaisesRegex(
                FuturesMainReportError, "日期必须存在于已验final raw"
            ):
                assess_with_independent_anchor(report, root, anchor)

        with self.subTest("degraded_cannot_claim_cache_replay"), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, _, _, anchor = build_case(root)
            report["market_data"]["latest_completed_bar"].update(
                {
                    "evidence_kind": "official_exchange_daily",
                    "provenance_status": "daily_cache_artifact_replay_required",
                }
            )
            with self.assertRaisesRegex(
                FuturesMainReportError, "degraded完成K线"
            ):
                assess_with_independent_anchor(report, root, anchor)

        with self.subTest("coordinated_resign_cannot_replace_independent_anchor"), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, frozen_path, final_path, anchor = build_case(root)
            frozen_raw = json.loads(frozen_path.read_text(encoding="utf-8"))
            final_raw = json.loads(final_path.read_text(encoding="utf-8"))
            for raw in (frozen_raw, final_raw):
                row = next(
                    item
                    for item in raw["technical"]["completed_daily_rows"]
                    if item["date"] == "2026-09-02"
                )
                row["low"] -= 1
            resign_frozen_structure_snapshot(report, frozen_path, frozen_raw)
            final_path.write_text(
                json.dumps(
                    final_raw,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            report["actual_main"]["snapshot_sha256"] = hashlib.sha256(
                final_path.read_bytes()
            ).hexdigest()
            with self.assertRaisesRegex(
                FuturesMainReportError, "冻结S1必须独立重放已锚定日线artifact"
            ):
                assess_with_independent_anchor(report, root, anchor)

        with self.subTest("coordinated_business_day_deletion_cannot_replace_anchor"), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, frozen_path, final_path, anchor = build_case(root)
            frozen_raw = json.loads(frozen_path.read_text(encoding="utf-8"))
            final_raw = json.loads(final_path.read_text(encoding="utf-8"))
            for raw in (frozen_raw, final_raw):
                raw["technical"]["completed_daily_rows"] = [
                    item
                    for item in raw["technical"]["completed_daily_rows"]
                    if item["date"] != "2026-09-04"
                ]
                raw["technical"]["daily_data_evidence"]["slice_row_count"] -= 1
            resign_frozen_structure_snapshot(report, frozen_path, frozen_raw)
            final_path.write_text(
                json.dumps(
                    final_raw,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            report["actual_main"]["snapshot_sha256"] = hashlib.sha256(
                final_path.read_bytes()
            ).hexdigest()
            with self.assertRaisesRegex(
                FuturesMainReportError, "冻结S1必须独立重放已锚定日线artifact"
            ):
                assess_with_independent_anchor(report, root, anchor)

    def test_resigned_degraded_daily_bytes_never_claim_cache_or_upstream_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = base_report()
            raw_path = bind_real_raw_and_structure(report, root)
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["technical"]["completed_daily_rows"][-1]["hold"] += 1
            report["market_data"]["latest_completed_bar"]["open_interest"] += 1
            resign_bound_raw_and_structure(report, raw_path, raw)
            result = _assess_report_at(
                report,
                load_policy(),
                validated_at=VALIDATED_AT,
                artifact_root=root,
            )
            self.assertTrue(result["completed_daily_bar_bound_to_raw_bytes"])
            self.assertFalse(result["daily_cache_artifact_replay_verified"])
            self.assertFalse(result["upstream_response_identity_verified"])

    def test_member_facts_use_official_sources_normalization_and_deleveraging(self):
        report = base_report()
        make_available_top20(report)
        self.assertEqual(self.assess(report)["evidence_status"]["member_flow"], "partial")

        wrong_denominator = deepcopy(report)
        wrong_denominator["member_flow"]["top20"]["daily"][0]["normalization"][
            "previous_total_open_interest"
        ] = 9600
        self.assert_rejected(wrong_denominator, "归一化分母")

        wrong_regime = deepcopy(report)
        wrong_regime["member_flow"]["top20"]["daily"][2]["flow_regime"] = (
            "long_build_short_cover"
        )
        self.assert_rejected(wrong_regime, "deleveraging")

        unofficial = deepcopy(report)
        unofficial["member_flow"]["top20"]["sources"][0]["url"] = (
            "https://example.com/member-ranking"
        )
        self.assert_rejected(unofficial, "官方域名")

        tampered_baseline = deepcopy(report)
        tampered_baseline["member_flow"]["prior_session_context"]["close"] = 99
        self.assert_rejected(tampered_baseline, "close_change")

if __name__ == "__main__":
    unittest.main()
