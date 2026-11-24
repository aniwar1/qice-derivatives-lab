"""展示从七月初按固定开仓预算独立结算的策略，以及北京时间当日的模型判断。"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from .agent_replay import BJT, GROUPS, digest, validate_decision
from .agent_replay_account import (
    FIXED_STRATEGY_VERSION,
    FIXED_TRADE_BUDGET,
    INITIAL,
    PER_LEG_BUDGET,
)
from .execution_research import select_order
from .july_policy import START, check_expiry, signal_policy
from .rolling_calendar import trading_days

RULES = {
    "A": (
        "多因子方向买权",
        [
            "选择一个实际月份的看涨或看跌期权。需要同品种直接供需证据和有传导依据的权威消息支持方向。",
            "目标价须在前 20 个交易日价格区间内。按目标内在价值、两边各 2% 不利滑点及费用测算，须有正的情景净收益。开盘再按实际权利金复核。",
        ],
    ),
    "B": (
        "技术择强买权",
        [
            "用实际月份合约的趋势、波动、成交与持仓选方向，买入一腿。相对排名领先本身不足以触发入场。",
            "同时核对基本面、消息面及反证。开盘触及失效位或目标止盈位时放弃该笔，买入价格还须符合委托限价。",
        ],
    ),
    "C": (
        "事件驱动宽跨式",
        [
            "用事前公开的时间表确认事件、相关品种及传导，要求拟持有期覆盖事件。默认在主力月份同时买入虚值看涨与看跌。",
            "看涨、看跌各有 5,000 元预算，按各自权利金、乘数和流动性分别买整手。两腿必须同时成交；预算余款留存。",
            "在此前 20 日区间内分别给出上下行情景。至少一侧内在价值须覆盖双腿权利金、压力滑点和费用，入场时用双腿开盘价再次检查。",
        ],
    ),
}

METHOD = (
    "三个模拟账户自 7 月 1 日开盘各以 200,000 元空仓起步。累计收益率按收盘权益相对各自初始本金计算，包含已实现及持仓盈亏。"
    "每组每天最多开一笔，单笔预算固定 10,000 元，权利金、1% 不利滑点及每腿每手 3 元开仓费计入上限。"
    "C 组双买分给看涨、看跌各 5,000 元；各腿按预算内可买的最大整数手委托，仅保留整手取整余款。各组总在手成本上限取前日权益的 8% 与 20,000 元中的较大值；现金、组合余量、手数或成交量不足以执行目标数量时，整笔放弃。"
    "使用实际月份合约的交易所日线模拟，平仓也计滑点及费用。"
)
RESEARCH_RULE = "核对消息初次可得时间、利益相关方、替代解释及同合约价量仓反应，注明已计价的证据和可证伪条件。"
EXIT_RULE = "持有期最长 5 个交易日。方向仓达到收盘失效或明确止盈条件，或仓位到达持有期限后，次一可成交开盘退出。"


def read_receipt(path):
    record = json.loads(path.read_bytes())
    packet = record["packet"]
    if (
        record["version"] != FIXED_STRATEGY_VERSION
        or packet.get("strategy_version") != FIXED_STRATEGY_VERSION
        or record["day"] != packet["trade_date"]
        or record["day"] != path.stem
        or record["cutoff"] != packet["cutoff"]
        or digest(packet) != record["context_sha256"]
        or digest(record["decisions"]) != record["decision_sha256"]
        or record["policy_sha256"] != digest(signal_policy(record["mode"]))
        or record["mode"] not in {"historical_replay", "preopen"}
        or not record["runtime"].get("run_id")
    ):
        raise ValueError(f"{path.stem} 策略回执版本、日期或摘要不符")
    cutoff = datetime.fromisoformat(record["cutoff"])
    if (
        cutoff.utcoffset() is None
        or cutoff.astimezone(BJT).date().isoformat() >= record["day"]
    ):
        raise ValueError(f"{path.stem} 盘前材料的截止时间无效")
    if record["mode"] == "preopen":
        recorded = datetime.fromisoformat(record["recorded_at"])
        if not cutoff <= recorded < cutoff + timedelta(hours=1) or digest(
            {k: v for k, v in record.items() if k != "sha256"}
        ) != record.get("sha256"):
            raise ValueError(f"{path.stem} 盘前决策封存时点或摘要无效")
    validate_decision(record["decisions"], packet)
    check_expiry(record["decisions"], packet)
    return record


def _display_order(day, record, decision):
    item = {
        "day": day,
        "status": "暂无当日决策",
        "product": "",
        "legs": [],
        "thesis": "",
        "technical": "",
        "fundamentals": "",
        "news": "",
        "invalidation": "",
        "evidence": [],
        "cutoff": "",
        "mode": "",
        "execution": None,
    }
    if decision is None:
        return item
    item.update({k: decision[k] for k in item if k in decision})
    item.update(
        status="等待" if decision["action"] == "wait" else "计划开仓",
        cutoff=record["cutoff"],
        mode=record["mode"],
        ownership=decision["ownership"],
        holding_days=decision["holding_days"],
        invalidation_price=decision["invalidation_price"],
        take_profit_close=decision["take_profit_close"],
        max_entry_premium=decision["max_entry_premium"],
        max_lots=decision["max_lots"],
        requested_budget_cny=FIXED_TRADE_BUDGET if decision["action"] == "enter" else 0,
        scenario_low=decision["scenario_low"],
        scenario_high=decision["scenario_high"],
        scenario_target=decision["scenario_target"],
    )
    packet = record["packet"]
    docs = {d["id"]: d for d in packet["documents"]}
    for ref in decision["evidence_ids"]:
        doc = docs.get(ref)
        if doc:
            item["evidence"].append(
                f"{doc.get('publisher', '')} {doc['title']}（{ref}）".strip()
            )
        else:
            source = packet["market_sources"][ref]
            item["evidence"].append(
                f"{source.get('day', source.get('month', ''))} {ref}".strip()
            )
    return item


def _today_decision(day, record, group, ledger, report_end, variant):
    sessions = ledger.get("sessions", [])
    session = sessions[-1] if sessions and sessions[-1]["day"] == day else None
    original = record["decisions"][group] if record else None
    item = _display_order(day, record, original)
    if session:
        item["execution"] = session["execution"]
    if not record or variant == "opening":
        return item
    if session:
        selected, source = session["decision"], session["order_source"]
    else:
        packet = record["packet"]
        account = packet.get("fixed_paper_accounts", {}).get(group)
        if account is None and report_end == packet.get("daily_data_date"):
            account = ledger.get("account")
        if account is None:
            item["selection_reason"] = "等待前一收盘的定额账户核对后生成有效委托。"
            return item
        selected, source = select_order(
            group, record["decisions"], packet, account["positions"], variant
        )
    item["effective_order"] = _display_order(day, record, selected)
    item["order_source"] = source
    reasons = []
    if source != group:
        reasons.append(f"采用 {source} 组已经提出的方向买权备选")
    if selected["holding_days"] != record["decisions"][source]["holding_days"]:
        reasons.append(
            f"按选用的退出规则将持有期调整为 {selected['holding_days']} 个交易日"
        )
    if not reasons:
        reasons.append("沿用原模型判断，按本组执行规则处理")
    item["selection_reason"] = "，".join(reasons) + "。"
    return item


def _update_issues(workspace, folder, report, now, has_settlement):
    issues = list(report["issues"])
    today = now.date().isoformat()
    if has_settlement:
        # 页面仅读取本地交易日历。每天 16 时到达收盘数据采集窗口。
        days = trading_days(workspace)
        due = next(
            (
                day
                for day in reversed(days)
                if day < today or (day == today and now.hour >= 16)
            ),
            None,
        )
        if due and due >= START and (not report["end"] or report["end"] < due):
            covered = report["end"] or "尚无"
            text = f"截至北京时间 {now:%m月%d日 %H:%M} 应结算至 {due}，当前曲线截至 {covered}。"
            if not any(f"最新应结算交易日 {due}" in issue for issue in issues):
                issues.append(text)
    path = folder / "daily-status.json"
    if path.is_file():
        status = json.loads(path.read_bytes())
        checked = datetime.fromisoformat(status["checked_at"]).astimezone(BJT)
        reason = status.get("reason")
        if (
            checked.date() == now.date()
            and status.get("status") == "pending"
            and reason
        ):
            replay_pending = any("的新版策略回执尚未完成" in issue for issue in issues)
            integration = reason in {
                "新版账目尚未结算至最近交易日",
                "待接续的账户与最近日表日期不一致",
            }
            resolved = report["end"] is not None and report["end"] == status.get(
                "last_settled"
            )
            if not (integration and (replay_pending or resolved)):
                issues.append(f"北京时间 {checked:%H:%M} 更新待完成：{reason}。")
    return issues


def overview(workspace: Path):
    folder = workspace / "local-data/july-strategy"
    now = datetime.now(BJT)
    day = now.date().isoformat()
    saved = folder / "settlement-v2.json"
    report = (
        json.loads(saved.read_bytes())
        if saved.is_file()
        else {
            "version": FIXED_STRATEGY_VERSION,
            "end": None,
            "groups": {},
            "issues": ["正在等待固定预算策略的首份日结。"],
        }
    )
    if report["version"] != FIXED_STRATEGY_VERSION:
        raise ValueError("结算版本与当前策略不一致")
    receipt_path = folder / "decisions-v2" / (day + ".json")
    issues = _update_issues(workspace, folder, report, now, saved.is_file())
    receipt = None
    if receipt_path.is_file():
        try:
            receipt = read_receipt(receipt_path)
        except (ValueError, OSError, KeyError, TypeError) as error:
            issues.append(f"{day} 的策略决策待核实：{error}")
    if receipt:
        products = receipt["packet"]["products"]
        no_options = [
            symbol for symbol, data in products.items() if not data["options"]
        ]
        if no_options:
            issues.append(
                f"{day} 的 {len(products)} 个研究品种中，{len(no_options)} 个缺少经核验的可选期权腿：{'、'.join(no_options)}。"
            )
    config = Path(__file__).resolve().parents[2] / "config"
    policy_rules = json.loads(
        (config / "full_budget_execution_study.json").read_bytes()
    )["rules"]
    validation_path = folder / "full-budget-execution-study/validation.json"
    validation = (
        json.loads(validation_path.read_bytes()) if validation_path.is_file() else {}
    )
    strategies = []
    for group in GROUPS:
        ledger = report["groups"].get(group, {})
        name, rules = RULES[group]
        variant = report.get("execution_policy", {}).get(group, "opening")
        rule = policy_rules[variant]
        if group == "B" and variant == "second_signal":
            rule = "同品种未退出时，须在新的交易日取得 B 组新的入场回执，才可再开一笔。每笔独立执行原限价、持有期限及退出，同品种最多同时两笔，两笔均须满足完整预算委托的现金与组合额度要求。"
        strategies.append(
            {
                "id": group,
                "name": name,
                "version": FIXED_STRATEGY_VERSION,
                "policy_id": variant,
                "rules": [*rules, RESEARCH_RULE, EXIT_RULE, rule],
                "curve": ledger.get("points", []),
                "decision": _today_decision(
                    day, receipt, group, ledger, report["end"], variant
                ),
                "opened": ledger.get("opened", 0),
                "closed": ledger.get("closed", 0),
            }
        )
        if variant != "opening" and validation.get("active", {}).get(group) == variant:
            stats = validation["groups"][group]
            if stats["candidate"] == variant:
                strategies[-1]["validation"] = {
                    "start": validation["period"][0],
                    "end": validation["period"][1],
                    "baseline": stats["opening"],
                    "selected": stats["selected"],
                }
        if group == "C":
            strategies[-1]["fills"] = [
                {
                    "day": session["day"],
                    "product": session["decision"]["product"],
                    "leg_fills": session["execution"]["leg_fills"],
                    "premium_and_fees": session["execution"]["premium_and_fees"],
                }
                for session in ledger.get("sessions", [])
                if session["execution"]["status"] == "simulated_open"
            ]
    return {
        "start": START,
        "end": report["end"],
        "as_of": day,
        "initial_capital": INITIAL,
        "trade_budget": FIXED_TRADE_BUDGET,
        "per_leg_budget": PER_LEG_BUDGET,
        "method": METHOD,
        "issues": issues,
        "strategies": strategies,
    }
