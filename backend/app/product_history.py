"""按品种串联当时可见的材料、模型原文和现场复核。"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from zipfile import ZipFile

from .agent_replay import BJT, digest
from .agent_replay_account import FIXED_STRATEGY_VERSION, SETTLEMENT_REVISION
from .rolling_view import _signed
from .study_view import BATCH, read_results
from .strategy_overview import read_receipt


def index(workspace: Path) -> dict:
    config = json.loads((workspace / "config/product_contract_specs.json").read_bytes())
    return {"products": [{k: p[k] for k in ("symbol", "name", "exchange")} for p in config["products"]]}


def _record(day, at, kind, title):
    return {"day": day, "at": at, "kind": kind, "title": title, "sections": [], "sources": []}


def _section(record, label, text):
    if isinstance(text, str) and text.strip():
        record["sections"].append({"label": label, "text": text.strip()})


def _source(record, title, url):
    if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
        return
    if not any(s["url"] == url and s["title"] == title for s in record["sources"]):
        record["sources"].append({"title": title or urlsplit(url).hostname, "url": url})


def _metrics(row, fields):
    return "，".join(f"{label} {row[k]}" for k, label in fields.items() if row.get(k) is not None)


def _packet_records(day, at, kind, packet, decisions):
    records = {}
    for symbol, data in packet.get("products", {}).items():
        tech = data.get("technical") or {}
        title = tech.get("contract") or symbol
        record = _record(day, at, kind, f"{title} · {'策略分析' if any(d and d.get('product') == symbol for d in decisions.values()) else '研究材料'}")
        records[symbol] = record
        _section(record, "研究时点", f"适用交易日 {packet.get('trade_date', day)}，行情日期 {packet.get('daily_data_date') or '未取得'}。")
        for group, decision in decisions.items():
            if not decision or decision.get("product") != symbol:
                continue
            action = {"wait": "观望", "enter": "计划入场"}.get(decision.get("action"), decision.get("action"))
            _section(record, f"{group} 组 · {action}", decision.get("thesis"))
            for field, label in (("technical", "技术判断"), ("fundamentals", "基本面"), ("news", "消息与传导"), ("ownership", "持仓归属"), ("invalidation", "失效条件")):
                _section(record, f"{group} 组 · {label}", decision.get(field))
        _section(record, "日线与持仓", _metrics(tech, {
            "close": "收盘", "volume": "成交量（手）", "open_interest": "持仓量（手）", "change5_pct": "五日涨跌（%）",
            "close_vs_sma20_pct": "偏离二十日均线（%）", "realized_vol20_ann_pct": "二十日年化波动率（%）",
            "low20": "二十日最低", "high20": "二十日最高",
        }) or "当时未取得可核对的本品种期货日线。")
        reactions = [
            _metrics(bar, {"day": "日期", "close": "收盘", "volume": "成交量", "open_interest": "持仓量"})
            for bar in data.get("reaction_history", [])
        ]
        _section(record, "同合约价量仓变化", "\n".join(reactions))
        legs = [f"{o['id']}　" + _metrics(o, {"premium": "权利金", "volume": "成交量（手）", "iv_pct": "隐含波动率（%）"}) for o in data.get("options", [])]
        if legs:
            _section(record, "当时期权合约", "\n".join(legs))
        for field, label in (("future", "期货月度"), ("option", "期权月度")):
            month = (data.get("monthly_aggregate") or {}).get(field) or {}
            _section(record, label, _metrics(month, {"month": "月份", "volume_lots": "月成交（手）", "month_end_open_interest": "月末持仓（手）"}))
        for source_id in data.get("source_ids", []):
            source = packet.get("market_sources", {}).get(source_id, {})
            _source(record, source_id, source.get("https") or source.get("source_url"))
        for doc in packet.get("documents", []):
            if symbol not in (doc.get("products") or []):
                continue
            _section(record, doc.get("title") or "公开资料", doc.get("excerpt"))
            _source(record, doc.get("title") or doc.get("publisher"), doc.get("url"))
        gaps = [g.get("reason") for g in packet.get("source_gaps", []) if isinstance(g, dict) and g.get("product") == symbol and g.get("reason")]
        _section(record, "证据缺口", "；".join(dict.fromkeys(gaps)))
    return records


@lru_cache(maxsize=300)
def _frozen(path: Path, stamp: int, mode: str, day: str, checksum: str = ""):
    if mode == "历史策略 · 原版回放":
        packet = json.loads(path.read_bytes())
        receipt = json.loads(path.with_name("decision.json").read_bytes())
        if (packet["trade_date"] != day or receipt["day"] != day or receipt["context_sha256"] != checksum
                or digest(packet) != checksum or digest(receipt["decisions"]) != receipt["decision_sha256"]):
            raise ValueError(f"{day} 归档回放摘要不符")
        return _packet_records(day, packet["cutoff"], mode, packet, receipt["decisions"])
    if mode == "新版策略":
        row = read_receipt(path)
        kind = "万元策略 · 盘前决策" if row["mode"] == "preopen" else "万元策略 · 历史回放"
        return _packet_records(day, row["cutoff"], kind, row["packet"], row["decisions"])
    row = _signed(path)
    packet = row["packet"]
    if mode == "历史策略 · 盘前决策":
        if packet["trade_date"] != day or digest(packet) != row["context_sha256"]:
            raise ValueError(f"{day} 现场回执不匹配")
        return _packet_records(day, row["recorded_at"], mode, packet, row["decisions"])
    if packet["trade_date"] != day:
        raise ValueError(f"{day} 收盘采集材料与适用交易日不符")
    return _packet_records(row["recorded_at"][:10], row["recorded_at"], mode, packet, {})


def _study(data, product, today):
    rows = []
    updated = data / "july-strategy/decisions-v2"
    days = set()
    for path in sorted(updated.glob("????-??-??.json")):
        if path.stem <= today:
            rows.extend(_for_symbol(_frozen(path, path.stat().st_mtime_ns, "新版策略", path.stem), product))
            days.add(path.stem)
    folder = data / "agent-study" / BATCH
    report = read_results(folder)
    if report["plans"]:
        for s in report["plans"][0]["sessions"]:
            day, key = s["day"], s["receipt_key"]
            if day in days or day > today or not s.get("receipted"):
                continue
            if not re.fullmatch("[a-f0-9]{64}", key):
                raise ValueError("历史回执索引无效")
            path = folder / "receipts" / key / "context.json"
            stamp = max(path.stat().st_mtime_ns, path.with_name("decision.json").stat().st_mtime_ns)
            rows.extend(_for_symbol(_frozen(path, stamp, "历史策略 · 原版回放", day, s["context_sha256"]), product))
    rolling = data / "rolling-six"
    for path in (rolling / "decisions").glob("????-??-??.json"):
        if path.stem <= today and path.stem not in days:
            rows.extend(_for_symbol(_frozen(path, path.stat().st_mtime_ns, "历史策略 · 盘前决策", path.stem), product))
    # 两套预采目录按适用交易日去重。正式决策已可见时，择取正式回执。
    handled = set(days)
    for source, kind in ((updated.parent, "万元策略 · 收盘资料采集"), (rolling, "收盘资料采集")):
        for folder in (source / "staged").glob("????-??-??"):
            if folder.name in handled:
                continue
            receipts = updated if source == updated.parent else source / "decisions"
            if folder.name <= today and (receipts / (folder.name + ".json")).is_file():
                continue
            path = max(folder.glob("*.json"), default=None)
            if path:
                records = _frozen(path, path.stat().st_mtime_ns, kind, folder.name)
                if product in records and records[product]["day"] <= today:
                    handled.add(folder.name)
                    rows.append(records[product])
    return rows


def _for_symbol(records, product):
    return [records[product]] if product in records else []


def _published(data, product):
    path = data / "qice.sqlite3"
    if not path.is_file():
        return []
    rows = []
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for (raw,) in db.execute("""
            SELECT r.result FROM research_publications p JOIN strategy_runs r ON p.run_id = r.id
            WHERE r.status = 'completed' AND r.result IS NOT NULL ORDER BY p.published_at
        """):
            body = json.loads(raw)
            s = body["strategy"]
            scan = next((r for r in s.get("scan_coverage", {}).get("products", []) if r.get("product") == product), None)
            if product != s.get("product") and not scan:
                continue
            at = body["updated_at"]
            record = _record(at[:10], at, "现场研究", f"{(scan or {}).get('actual_main_contract') or product} · {'专项研判' if s.get('product') == product else '全市场扫描'}")
            _section(record, "扫描覆盖", (scan or {}).get("known_gap"))
            if s.get("product") == product:
                for k, label in (("thesis", "结论"), ("why_now", "当前时机"), ("why_not_wait", "等待条件")):
                    _section(record, label, s.get("rationale", {}).get(k))
                for k, label in (("technical_structure", "技术结构"), ("fundamentals_news", "基本面与消息"), ("member_flow", "会员持仓"), ("option_market", "期权市场")):
                    item = s.get(k) or {}
                    _section(record, label, item.get("conclusion"))
                    for src in item.get("sources", []):
                        _source(record, src.get("title") or label, src.get("url"))
                _section(record, "风险", "\n".join(s.get("risks", [])))
                _section(record, "证据缺口", "\n".join(s.get("evidence_gaps", [])))
                _section(record, "反向论证", (s.get("quality_review") or {}).get("strongest_challenge"))
            if record["sections"]:
                rows.append(record)
    finally:
        db.close()
    return rows


def _quote_review(folder, product):
    rows = []
    for path in folder.glob("reviews/*/review.json"):
        review = json.loads(path.read_bytes())
        at = review.get("observed_at") or review.get("verified_at") or review.get("actual_run_at") or review["nominal_bjt"]
        for market in review.get("market", []):
            if market.get("product") != product:
                continue
            contract = market.get("underlying_contract") or product
            record = _record(at[:10], at, "纸面研究复核", f"{contract} · 日间复核")
            bars = market.get("bars") or []
            if bars:
                _section(record, "完成日线", _metrics(bars[-1], {"date": "行情日", "open": "开盘", "high": "最高", "low": "最低", "close": "收盘", "volume": "成交量", "open_interest": "持仓量"}))
            for score in market.get("direction_scores", []):
                side = "看多" if score["direction"] == "long" else "看空"
                _section(record, f"{side} · 已核项目", _metrics(score, {"score": "研究评分", "certainty_score": "确定性得分", "opportunity_score": "机会得分", "missing_input_weight": "缺失权重"}))
            for key in ("A", "B"):
                _section(record, f"{key} 组 · 全市场结论", (review.get("AB_review") or review.get("AB_opposing_case") or {}).get(key))
            _section(record, "C 组 · 全市场结论", (review.get("C_review") or {}).get("decision") or review.get("C_reason"))
            if not bars:
                _section(record, "覆盖状态", "本次复核缺少已完成日线。")
            for s in market.get("sources", []):
                _source(record, "行情来源", s.get("source_url") or s.get("url"))
            rows.append(record)
        night = review.get("night_review", {})
        quotes = night.get("night_quotes", [])
        if night.get("most_recent_SP_quote"):
            quotes = [*quotes, night["most_recent_SP_quote"]]
        for q in quotes:
            symbol = q.get("contract") or q.get("symbol") or ""
            if symbol.rstrip("0123456789") != product:
                continue
            record = _record(at[:10], at, "纸面研究复核", f"{symbol} · 夜盘复核")
            _section(record, "夜盘报价", _metrics(q, {"price": "价格", "trade": "价格", "quote_time": "时点", "ticktime": "时点", "volume": "成交量"}))
            _section(record, "跟踪判断", review.get("morning_watch_follow_up") or q.get("scope"))
            _source(record, "夜盘行情", (night.get("quote_source") or {}).get("url"))
            rows.append(record)
    return rows


_RUBRIC = {
    "catalyst_transmission": "事件传导", "counterevidence_resilience": "反证", "expiry_horizon": "到期与持有期",
    "iv_percentile": "波动率", "key_member_alignment": "重点席位", "marginal_change": "边际变化",
    "supply_demand_balance": "供需平衡", "top20_position_change": "前二十席位变化",
}


def _ledger_records(folder, product):
    ledger = folder / "ledger.jsonl"
    if not ledger.is_file():
        return []
    rows = []
    with ledger.open() as stream:
        for line in stream:
            event = json.loads(line)
            body = event["payload"]
            at = event["recorded_at"]
            if event["kind"] == "assumed_plan_registered" and body.get("product") == product:
                row = _record(at[:10], at, "历史纸面计划", f"{body.get('option_symbol') or product} · {event.get('group') or body.get('group') or ''}组")
                _section(row, "计划依据", body.get("rationale") or body.get("reason"))
                _section(row, "最强反证", body.get("strongest_counterevidence"))
                _section(row, "交易日", body.get("official_trading_day"))
                rows.append(row)
            if event["kind"] != "rank_snapshot":
                continue
            for candidate in body.get("candidates", []):
                if candidate.get("product") != product:
                    continue
                row = _record(at[:10], at, "历史品种评分", f"{candidate.get('underlying_contract') or product} · 当时评分")
                _section(row, "截至当时", _metrics(candidate, {"price_date": "行情日", "underlying_close": "收盘", "rank": "排名", "score": "研究评分", "missing_input_weight": "缺失权重"}))
                _section(row, "技术指标", _metrics(candidate.get("features") or {}, {"ma5": "五日均价", "ma20": "二十日均价", "ma60": "六十日均价", "atr20": "二十日平均真实波幅", "volume_ratio": "量比"}))
                for key, info in (candidate.get("review_signals") or {}).items():
                    if isinstance(info, dict):
                        _section(row, _RUBRIC.get(key, key), info.get("conclusion"))
                        for s in info.get("sources") or []:
                            if isinstance(s, dict):
                                _source(row, _RUBRIC.get(key, key), s.get("source_url") or s.get("url"))
                _section(row, "记录说明", candidate.get("reason"))
                if row["sections"]:
                    rows.append(row)
    return rows


def _dossiers(data, symbol):
    records = {}
    for source in sorted((data / "product-dossiers" / symbol / "app-snapshots").glob("*/source.json")):
        metadata = json.loads(source.read_bytes())
        if metadata.get("product") != symbol:
            continue
        archive = source.with_name(f"qice-{symbol}.zip").read_bytes()
        if sha256(archive).hexdigest() != metadata["archive_sha256"]:
            raise ValueError(f"{symbol} 品种档案与原包摘要不符")
        with ZipFile(BytesIO(archive)) as bundle:
            versions = json.loads(bundle.read("analysis.json"))
        for version in versions:
            if version["product"] != symbol:
                continue
            at = datetime.fromisoformat(version["evidence_as_of"].replace("Z", "+00:00")).astimezone(BJT).isoformat()
            payload = version["payload"]
            record = _record(at[:10], at, "品种研究档案", f"{payload.get('underlying_contract') or symbol} · 研究版本 {version['id']}")
            _section(record, "摘要", version.get("summary"))
            _section(record, "完整研判", payload.get("report"))
            _section(record, "判断依据", payload.get("rationale"))
            _section(record, "证据缺口", "\n".join(payload.get("evidence_gaps") or []))
            review = payload.get("quality_review") or {}
            _section(record, "反向论证", review.get("strongest_challenge"))
            _section(record, "复核结论", review.get("resolution"))
            for item in payload.get("sources") or []:
                _source(record, "原始证据", item.get("url"))
            _source(record, "品种原始档案", metadata.get("preview_note_link"))
            records[version["id"]] = record
    return list(records.values())


def _simulated_trades(data, product):
    path = data / "july-strategy/settlement-v2.json"
    if not path.exists():
        return []
    report = json.loads(path.read_bytes())
    if report.get("version") != FIXED_STRATEGY_VERSION or report.get("settlement_revision") != SETTLEMENT_REVISION:
        return []
    rows = []
    for group, account in report["groups"].items():
        for session in account["sessions"]:
            day = session["day"]
            execution = session["execution"]
            order = session.get("decision") or {}
            if order.get("product") == product and order.get("action") == "enter":
                opened = execution["status"] == "simulated_open"
                title = f"{group} 组 · {product} · " + ("模拟开仓" if opened else "委托未成交")
                row = _record(day, None, "模拟委托记录", title)
                _section(row, "盘前委托", " / ".join(order["legs"]))
                _section(row, "成交依据" if opened else "未成交原因", execution.get("reason"))
                _section(row, "模拟开仓", _metrics(execution, {"quantity": "手数", "budget_cny": "预算（元）", "premium_and_fees": "含费成本（元）"}))
                if len(dates := execution.get("same_product_entry_dates", [])) > 1:
                    _section(row, "独立信号", "本轮为第二笔独立委托，当前两笔的开仓日为" + "、".join(dates) + "。")
                for leg in execution.get("leg_fills", []):
                    _section(row, leg["leg"], _metrics(leg, {"quantity": "手数", "budget_cny": "预算（元）", "premium_and_fees": "含费成本（元）"}))
                rows.append(row)
            for close in execution["exits"]:
                if close["product"] != product:
                    continue
                row = _record(day, None, "模拟委托记录", f"{group} 组 · {product} · 模拟平仓")
                _section(row, "合约", " / ".join(close["legs"]))
                _section(row, "开仓日", close["entry_day"])
                _section(row, "平仓原因", close["reason"])
                _section(row, "扣费后损益", _metrics(close, {"quantity": "手数", "proceeds_after_costs": "净回收（元）", "pnl": "净盈亏（元）"}))
                for leg in close.get("leg_fills", []):
                    _section(row, leg["leg"], _metrics(leg, {"quantity": "手数", "opening_debit": "开仓成本（元）", "pnl": "净盈亏（元）"}))
                rows.append(row)
    return rows


def detail(workspace: Path, product: str) -> dict:
    symbol = product.upper()
    config = {p["symbol"]: p for p in index(workspace)["products"]}
    product_info = config[symbol]
    data = workspace / "local-data"
    today = datetime.now(BJT).date().isoformat()
    forward = Path(os.environ.get("QICE_FORWARD_PAPER_ROOT") or data / "forward-paper")
    rows = _study(data, symbol, today)
    rows.extend(_published(data, symbol))
    rows.extend(_quote_review(forward, symbol))
    rows.extend(_ledger_records(forward, symbol))
    rows.extend(_dossiers(data, symbol))
    rows.extend(_simulated_trades(data, symbol))
    rows.sort(key=lambda r: (r["day"], r["at"] or ""), reverse=True)
    return {"product": symbol, "name": product_info["name"], "exchange": product_info["exchange"], "records": rows}
