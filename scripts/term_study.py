"""使用冻结行情和事前公布的事件日历比较两个期限的同品种宽跨买方组合。"""
import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, time
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from app.agent_replay import BJT, digest, positive
from app.term_decay import explain_decay
from app.option_expiry import ExpiryIndex
from app.term_study import RISK_BUDGET, execution, select_term_pair, summarize
from scripts.agent_replay import atomic

ROOT = Path(__file__).resolve().parents[1]


def event_slots(study, trading_days):
    db_path = study / "source-snapshot/facts.sqlite3"
    slots = defaultdict(lambda: defaultdict(list))
    verified = set()
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        for raw, in conn.execute("SELECT data FROM facts WHERE scheduled IS NOT NULL ORDER BY scheduled,symbol,fact_id"):
            doc = json.loads(raw)
            event = datetime.fromisoformat(doc["scheduled_at"]).astimezone(BJT)
            if not trading_days[0] <= event.date().isoformat() <= trading_days[-1]:
                continue
            if doc["sha256"] not in verified:
                blob = study / "source-snapshot/blobs" / doc["sha256"]
                if sha256(blob.read_bytes()).hexdigest() != doc["sha256"]:
                    raise ValueError("日程原件摘要不符")
                verified.add(doc["sha256"])
            for product in doc["products"]:
                slots[event][product].append(doc)
    return slots


def timings(event, calendar):
    # 双腿日开盘可能包含夜盘或较晚的首笔成交。整日严格放在事件之前或之后。
    index = bisect_left(calendar, event.date().isoformat())
    if index < 2 or index + 1 >= len(calendar):
        return None
    return calendar[index - 2], calendar[index - 1], calendar[index + 1]


def run(study, anchor="future"):
    run_manifest = json.loads((study / "run-manifest.json").read_bytes())
    manifest = json.loads((study / "market/source-manifest.json").read_bytes())
    packed = (study / "market/market.json.gz").read_bytes()
    db_hash = sha256((study / "source-snapshot/facts.sqlite3").read_bytes()).hexdigest()
    if (sha256(packed).hexdigest() != run_manifest["market_sha256"]
            or digest(manifest) != run_manifest["market_manifest_sha256"]
            or db_hash != run_manifest["source_snapshot_sha256"]):
        raise ValueError("冻结行情、来源索引或日程库的摘要不一致")
    market = json.loads(gzip.decompress(packed))
    calendar = manifest["calendar"]
    days = run_manifest["calendar"]
    if len(days) <= 42:
        raise ValueError("缺少独立的前段研究窗口")
    split = days[-42]
    specs_path = ROOT / "config/product_contract_specs.json"
    specs_raw = specs_path.read_bytes()
    specs = {row["symbol"]: row["multiplier"] for row in json.loads(specs_raw)["products"]}
    universe = manifest["configured_universe"]
    sources = {(r["day"], r["exchange"], r["type"]): r for r in manifest["sources"]}
    # 郑商所的 HTTP 版本时间单独归档，须与行情清单及网络回读摘要匹配。
    vintages_raw = (study / "market/vintage-verification.json").read_bytes()
    czce_times = {}
    for receipt in json.loads(vintages_raw)["files"]:
        if (not receipt.get("issues") and receipt["expected_sha256"] == receipt["archived_sha256"]
                == receipt["observed_sha256"]):
            czce_times[receipt["url"], receipt["expected_sha256"]] = receipt["last_modified_cst"]
    reference_path = study.parent / (study.name + "-frequency") / "expiry-index.json"
    reference = ExpiryIndex(reference_path)
    events = event_slots(study, days)
    pairs, gaps, clusters = [], [], []
    for event, products in sorted(events.items()):
        stamp = event.isoformat()
        dates = timings(event, calendar)
        if not dates or dates[1] < days[0] or dates[2] > days[-1]:
            gaps.extend({"event_at": stamp, "product": p, "reason": "window_boundary"} for p in products)
            continue
        prior, entry, exit_day = dates
        cutoff = datetime.combine(date.fromisoformat(prior), time(20), BJT)
        subset = "holdout" if entry >= split else "initial" if exit_day < split else "cross_boundary"
        group = {"event_at": stamp, "selection_cutoff": cutoff.isoformat(), "entry": entry, "exit": exit_day,
                 "segment": subset}
        expiries = {leg: reference.lookup(leg, cutoff) for leg in market[prior]["option"]}
        scoped = []
        for product, records in sorted(products.items()):
            docs = [d for d in records if datetime.fromisoformat(d["available_at"]) <= cutoff]
            descriptor = {**group, "product": product, "reports": sorted({d["title"] for d in docs}),
                          "source_ids": [d["id"] for d in docs]}
            def gap(reason, **details):
                gaps.append({**descriptor, "reason": reason, **details})
            if not docs:
                gap("schedule_not_known_at_cutoff")
                continue
            exchange = universe[product]
            source_keys = [(prior, exchange, "future"), (prior, exchange, "option"),
                           (entry, exchange, "option"), (exit_day, exchange, "option")]
            evidence = []
            reason = None
            for key in source_keys[:2]:
                record = sources.get(key, {})
                if not record.get("configured_product_rows", {}).get(product):
                    reason = "missing_prior_" + key[2]
                    break
                at = record.get("disclosed_available_at")
                if exchange == "CZCE" and not at:
                    at = czce_times.get((record["https"], record["sha256"]))
                if not at or datetime.fromisoformat(at) > cutoff:
                    reason = "prior_publication_unconfirmed_or_late"
                    break
                evidence.append(record["sha256"])
            if reason:
                gap(reason)
                continue
            market_day = market[prior]
            matched, reason = select_term_pair(product, market_day["future"], market_day["option"],
                                               cutoff.date(), date.fromisoformat(exit_day), specs[product], anchor=anchor, expiries=expiries)
            if not matched:
                gap(reason)
                continue
            # 是否存在退出行情不参与选月，只在冻结双腿和数量后评估。
            for key in source_keys[2:]:
                record = sources.get(key, {})
                evidence.append(record.get("sha256") if record.get("configured_product_rows", {}).get(product) else None)
            observations = [market[d]["option"] if sha else {} for d, sha in zip((entry, exit_day), evidence[2:])]
            for name in ("main", "far"):
                structure = matched[name]
                structure["execution"] = execution(structure, *observations, specs[product])
                # 两条期货路径仅作事后解释，标的价不得替代期权腿的权利金。
                underlying_opens = {}
                for label, day in (("entry", entry), ("exit", exit_day)):
                    bar = market[day]["future"].get(structure["contract"])
                    receipt = sources.get((day, exchange, "future"))
                    backed = receipt and receipt.get("configured_product_rows", {}).get(product)
                    underlying_opens[label] = {"open": bar[0], "sha256": receipt["sha256"]} if (
                        backed and bar and positive(bar[0]) and positive(bar[4])) else None
                structure["underlying_opens"] = underlying_opens
            detail = {**matched, "price_source_hashes": dict(zip(("decision_future", "decision_option", "entry", "exit"), evidence)),
                      "multiplier": specs[product]}
            if any(matched[name]["execution"]["status"] != "priced" for name in ("main", "far")):
                gap("not_both_executable", **detail)
                continue
            row = {**descriptor, **detail}
            scoped.append(row)
            pairs.append(row)
        clusters.append({**group, "eligible_products": len(products), "reports": sorted({d["title"] for ds in products.values() for d in ds
                                      if datetime.fromisoformat(d["available_at"]) <= cutoff}), **summarize(scoped)})
    summary = {"one_day_decay": explain_decay(pairs, RISK_BUDGET), "full": summarize(pairs),
               **{name: summarize([p for p in pairs if p["segment"] == name])
                  for name in ("initial", "holdout", "cross_boundary")},
               "products_considered": len(universe), "product_event_observations": len(pairs) + len(gaps),
               "gap_counts": dict(Counter(g["reason"] for g in gaps)), "scheduled_clusters": len(events)}
    return {"schema_version": 1, "window": {"start": days[0], "end": days[-1], "holdout_start": split,
                                             "sessions": len(days), "holdout_sessions": 42},
            "policy": {"anchor": anchor,
                       "decay_model": "按选择日前收和真实到期日逐腿反解 Black-76 隐波，利率设零，固定各月份期货价格与隐波重估少一天的权利金，按原预算手数折算。该模型忽略提前行权和实际波动变化，只作理论诊断。", "risk_budget_yuan_per_structure": RISK_BUDGET, "slippage_each_side_pct": 1,
                       "fee_yuan_per_leg_lot_each_side": 3, "prior_volume_participation_pct": 1,
                       "max_otm_difference_pp": 1, "target_otm_pct": 2, "allowed_otm_pct": [0.5, 5],
                       "rule": ("前日全行权价看涨和看跌合计持仓量最大月份为主力，持仓并列按成交量。 " if anchor == "option" else "按前日真实期货持仓量取主力。") + "以各月份自身期货价格选虚值看涨和看跌；远月从最近的后续交割月开始，须同时满足两腿虚值、成交量、持仓、到期缓冲和预算。未配对保留原因。",
                       "expiry": "用当时已公布的交易所逐腿原始合约表核验到期日，两腿均须覆盖整个持有窗口并晚于预设退出日；另沿用提前于交割前月首日退出的保守筛选。",
                       "fallback": "配对检验只在两个月份均可行时比较。某月报价不足不代表另一月也不能成交；现场若确有其他期限，应独立核验事件、成本、真实到期日与同月份标的。",
                       "timing": "事件前最后完整交易日开盘代理买入。事件日或其后首个交易日的下一交易日开盘代理卖出。各腿的首笔成交可能不同步。平仓日未能取得价格的候选计入缺口。",
                       "sizing": "在前一交易日20:00冻结腿和数量；按权利金、费用及退出费用准备金检查实际开盘价和整日成交量，预算不足则拒绝模拟成交。",
                       "unit": "收益按每个组合盈亏÷2000元预算计算。按同一发布时间内配对品种等权、不同发布时间等权。各样本独立占用预算，无组合净值或复利推论。",
                       "scope": "已知国家统计局宏观日程的独立配对实验，各事件实际公布值不进入选月。跨分段持仓只计入全期。"},
            "summary": summary, "pairs": pairs, "gaps": gaps, "clusters": clusters,
            "sources": {"expiry_index_sha256": sha256(reference_path.read_bytes()).hexdigest(), "market_sha256": manifest["data_sha256"], "market_manifest_sha256": digest(manifest),
                        "source_snapshot_sha256": db_hash, "czce_vintage_sha256": sha256(vintages_raw).hexdigest(), "contract_specs_sha256": sha256(specs_raw).hexdigest(),
                        "event_urls": sorted({d["url"] for source in events.values() for docs in source.values() for d in docs})}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--anchor", choices=["future", "option"], default="future")
    args = parser.parse_args()
    if args.out.resolve().is_relative_to(args.study.resolve()):
        parser.error("请另选输出目录以保护冻结的原研究")
    result = run(args.study, args.anchor)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    atomic(args.out, result)
    print(json.dumps(result["summary"], ensure_ascii=False))
