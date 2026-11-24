"""使用已冻结的来源档案重建逐日 Agent 决策，并独立评估三个模拟组合。"""

import fcntl
import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import time as clock
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time
from hashlib import sha256
from pathlib import Path

from app.agent_replay import (
    BJT,
    GROUPS,
    POLICY,
    build_packet,
    digest,
    validate_decision,
    wire_schema,
)
from app.agent_replay_account import Account
from app.agent_replay_model import decide

ROOT = Path(__file__).resolve().parents[1]
ASSUMPTIONS = [
    "每个交易日为 22 个品种冻结同一时点的证据，3 个组合分别判断，允许空仓。",
    "模型每天独立提出市场判断与净值风险基点，输入不含其他日期的模型结论或账户盈亏。账户按时序用当时权益和在手成本控制新仓。已有仓位沿初始计划管理，未交给模型逐日重评。",
    "周末与节假日沿前一交易日 20:00 冻结，此后至日盘开盘新增的消息在下一决策时点才进入。",
    "A 可基于权威供需、消息和目标内在价值提出成本情景压力模拟，目标位与潜在盈利均为假设。",
    "以交易所披露的原件更新时点校验可用性。已知迟于日终 20:00 的行情不触发该次决策或收盘止盈止损。缺少更新时点的归档按日终可用重建，历史逐分钟版本并不完整。",
    "合约乘数来自本机合约规格档案，历史逐日有效版本尚未独立核验。",
    "每个组合起始现金 20 万元。新仓权利金含费用不超过前日权益的 1%，总在手成本不超过 8%。同组合、同品种最多一笔在手。",
    "同月份实际合约公开开盘价为代理。每腿、每边增加 1% 不利滑点和每手 3 元费用。",
    "数量至多为前一日最低腿成交量的 1%，事后全天量不足时整单撤销。缺少逐笔盘口、双腿同步成交与真实自营账户持仓证据。",
    "C 另给出事前上下价格情景，限定在近 20 日范围内。双腿买卖各按 2% 不利滑点与双边费用计算两尾内在价值净损益。至少一尾覆盖总成本才按开盘代理复核，两种情景均无概率假设。",
    "B 未引用该品种近期基本面或消息时，实际预算再限于 50 基点。止盈价、手数和开盘权利金上限由模型单独给出，收盘触发后次开退出。情景目标本身仅用于测算。",
    "持仓达到 Agent 预定期限或收盘触及失效价，则在下一可用开盘退出。末日持仓计收盘浮盈亏，缺少当日价续用前值并记录。",
]


def atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, mode="w", encoding="utf-8", delete=False
    ) as file:
        json.dump(obj, file, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(file.name, path)


def load(path):
    return json.loads(path.read_bytes())


def identity(packet, model, *, runner=None):
    # 冻结模型、规则、契约及全部上下文，防止跨版本误复用。
    return digest(
        {
            "context": packet,
            "model": model,
            "policy": POLICY,
            "schema": wire_schema(packet["products"]),
            "runner": runner or sha256(
                (ROOT / "backend/app/agent_replay_model.py").read_bytes()
            ).hexdigest(),
        }
    )


def matches_archived_identity(packet, model, key):
    # 旧版档案冻结了运行器源码摘要，保留当时的完整身份校验。
    return key in (
        identity(packet, model),
        identity(packet, model, runner="a45d81a97db6b255c0c8fb1991e58f44579887c3455f55fae19496c90ace0806"),
    )


def read_receipt(path, packet, model, key):
    saved = load(path)
    if not (
        saved["key"] == key
        and saved["context_sha256"] == digest(packet)
        and saved["cutoff"] == packet["cutoff"]
        and saved["day"] == packet["trade_date"]
        and saved["decision_sha256"] == digest(saved["decisions"])
        and saved["runtime"]["model"] == model
        and saved["runtime"]["run_id"]
    ):
        raise ValueError("已有模型回执校验失败")
    validate_decision(saved["decisions"], packet)
    return saved


def execute_one(study, packet, model, cli):
    day = packet["trade_date"]
    key = identity(packet, model)
    base = study / "receipts" / key
    path = base / "decision.json"
    if path.exists():
        return read_receipt(path, packet, model, key)
    atomic(base / "context.json", packet)
    error = None
    for attempt in range(2):
        try:
            decisions, runtime = decide(
                packet, model, cli, feedback=error if attempt else None
            )
            out = {
                "day": day,
                "cutoff": packet["cutoff"],
                "key": key,
                "context_sha256": digest(packet),
                "decisions": decisions,
                "decision_sha256": digest(decisions),
                "runtime": runtime,
            }
            atomic(path, out)
            return out
        except (RuntimeError, TimeoutError, ValueError) as exc:
            error = str(exc)
            if attempt == 0:
                clock.sleep(2)
    raise RuntimeError(error)


def evaluation(
    study, days, packets, receipts, market, manifest, model, errors, missing_sources
):
    # 账户路径不可跨过任何无决策的日子；只计算按序连续的已完成交易日。
    complete = len(receipts) == len(days)
    plans = []
    base_status = "complete" if complete else "partial"
    delayed_futures = {}
    for source in manifest["sources"]:
        timestamp = source.get("disclosed_available_at")
        if source["type"] == "future" and timestamp:
            observation = datetime.combine(
                date.fromisoformat(source["day"]), time(20), tzinfo=BJT
            )
            if datetime.fromisoformat(timestamp) > observation:
                delayed_futures.setdefault(source["day"], set()).add(source["exchange"])
    for group in GROUPS:
        account = Account(group)
        curve, sessions = [], []
        for day in days:
            row = receipts.get(day)
            if not row:
                break
            packet = packets[day]
            decision = row["decisions"][group]
            execution, point = account.process(
                day, decision, packet, market[day], delayed_futures.get(day, ())
            )
            curve.append(point)
            sessions.append(
                {
                    "day": day,
                    "cutoff": packet["cutoff"],
                    "decision": decision,
                    "execution": execution,
                    "context_sha256": row["context_sha256"],
                    "receipt_key": row["key"],
                    "receipted": True,
                }
            )
        last = curve[-1] if curve else {}
        stats = {
            k: last.get(k)
            for k in ("equity", "return_pct", "realized", "open_pnl", "open_positions")
        }
        stats.update(
            drawdown_pct=100 * account.drawdown if curve else None,
            opened=account.opened,
            closed=account.closed,
            strict_verified_fills=0,
        )
        plans.append(
            {"id": group, "curve": curve, "sessions": sessions, "stats": stats}
        )
    hits = {
        p: {"technical": 0, "option_chain": 0, "fundamentals": 0, "news": 0}
        for p in manifest["configured_universe"]
    }
    for packet in packets.values():
        for p, item in packet["products"].items():
            hits[p]["technical"] += int(bool(item["technical"]))
            hits[p]["option_chain"] += int(bool(item["options"]))
            for field in ("fundamentals", "news"):
                hits[p][field] += int(
                    any(
                        doc["dimension"] == field and p in doc["products"]
                        for doc in packet["documents"]
                    )
                )
    report = {
        "schema_version": 1,
        "status": base_status,
        "planned_days": len(days),
        "completed_days": len(receipts),
        "evaluated_days": len(plans[0]["sessions"]),
        "products": list(hits),
        "start": days[0],
        "end": days[-1],
        "model": model,
        "assumptions": ASSUMPTIONS,
        "plans": plans,
        "errors": errors,
        "context_coverage": hits,
        "missing_source_days": missing_sources,
        "market_sha256": manifest["data_sha256"],
    }
    atomic(study / "results.json", report)
    return report


def frozen_sources(study, source):
    # 一次事务复制索引。原文以内容摘要保存，查询时再逐条核验。
    destination = study / "source-snapshot"
    if destination.is_dir():
        if (
            not (destination / "facts.sqlite3").is_file()
            or not (destination / "blobs").is_dir()
        ):
            raise ValueError("已冻结的来源快照不完整")
        return destination
    source = source.resolve()
    with tempfile.TemporaryDirectory(dir=study) as tmp:
        staging = Path(tmp) / "snapshot"
        staging.mkdir(mode=0o700)
        file = source / "facts.sqlite3"
        with (
            sqlite3.connect(file.as_uri() + "?mode=ro", uri=True) as original,
            sqlite3.connect(staging / "facts.sqlite3") as snapshot,
        ):
            original.backup(snapshot)
        os.chmod(staging / "facts.sqlite3", 0o600)
        shutil.copytree(source / "blobs", staging / "blobs")
        if (source / "supplement/index.json").is_file():
            shutil.copytree(source / "supplement", staging / "supplement")
        os.replace(staging, destination)
    return destination


def run(args):
    from akshare.futures.cons import get_calendar
    from app.agent_sources import lookup_agent_sources

    study = args.study.resolve()
    manifest = load(study / "market/source-manifest.json")
    raw = (study / "market/market.json.gz").read_bytes()
    if sha256(raw).hexdigest() != manifest["data_sha256"]:
        raise ValueError("市场输入摘要不符")
    market = json.loads(gzip.decompress(raw))
    calendar = manifest["calendar"]
    if calendar != sorted(set(calendar)) or set(calendar) != set(market):
        raise ValueError("交易所日历与市场数据日键不一致")
    days = [d for d in calendar if args.start <= d <= args.end]
    if not days or calendar.index(days[0]) < 1:
        raise ValueError("所选区间或前一交易日缺失")
    specs = {
        r["symbol"]: r
        for r in load(ROOT / "config/product_contract_specs.json")["products"]
    }
    universe = manifest["configured_universe"]
    if set(universe) != set(specs):
        raise ValueError("研究范围须覆盖全部已配置品种")
    source_index = {
        (r["day"], r["exchange"], r["type"]): r for r in manifest["sources"]
    }
    all_sessions = sorted(date.fromisoformat(x).isoformat() for x in get_calendar())
    snapshot = frozen_sources(study, args.sources)
    monthly = load(study / "monthly/index.json")
    packets, receipts, errors, missing_sources = {}, {}, {}, []
    for day in days:
        prior = calendar[: calendar.index(day)]
        cutoff = datetime.combine(date.fromisoformat(prior[-1]), time(20), tzinfo=BJT)
        docs = lookup_agent_sources(
            list(universe), archive_root=snapshot, cutoff=cutoff
        )
        items = docs["items"]
        if not items:
            missing_sources.append(day)
        packets[day] = build_packet(
            day, prior, market, source_index, universe, specs, items, monthly=monthly
        ) | {
            "next_sessions": all_sessions[
                all_sessions.index(day) : all_sessions.index(day) + 5
            ],
            "authority_policy": docs["authority_policy"],
            "positioning_policy": docs["positioning_policy"],
            "source_gaps": docs["gaps"],
            "source_vintage": docs["vintage_note"],
        }
    atomic(
        study / "run-manifest.json",
        {
            "start": args.start,
            "end": args.end,
            "calendar": days,
            "model": args.model,
            "market_sha256": manifest["data_sha256"],
            "market_manifest_sha256": digest(manifest),
            "source_snapshot_sha256": sha256(
                (snapshot / "facts.sqlite3").read_bytes()
            ).hexdigest(),
            "supplement_sha256": sha256(
                (snapshot / "supplement/index.json").read_bytes()
            ).hexdigest()
            if (snapshot / "supplement/index.json").is_file()
            else None,
            "monthly_sha256": digest(monthly),
            "contexts": {d: identity(p, args.model) for d, p in packets.items()},
        },
    )
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tasks = {
            pool.submit(execute_one, study, p, args.model, args.cli): day
            for day, p in packets.items()
        }
        for task in as_completed(tasks):
            day = tasks[task]
            try:
                receipts[day] = task.result()
                print(day, "model_complete", len(receipts), "/", len(days), flush=True)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                errors[day] = str(exc)
                print(day, "model_error", str(exc), flush=True)
            evaluation(
                study,
                days,
                packets,
                receipts,
                market,
                manifest,
                args.model,
                errors,
                missing_sources,
            )
    if errors:
        raise SystemExit(1)


def settle_frozen(study):
    original = load(study / "run-manifest.json")
    manifest = load(study / "market/source-manifest.json")
    raw = (study / "market/market.json.gz").read_bytes()
    if original.get("market_manifest_sha256", digest(manifest)) != digest(manifest):
        raise ValueError("冻结后的行情来源清单改变")
    if (
        sha256(raw).hexdigest() != original["market_sha256"]
        or manifest["data_sha256"] != original["market_sha256"]
    ):
        raise ValueError("冻结后的行情摘要改变")
    market = json.loads(gzip.decompress(raw))
    days, model = original["calendar"], original["model"]
    if set(days) != set(original["contexts"]) or len(days) != len(set(days)):
        raise ValueError("冻结任务时点与日历不匹配")
    packets, receipts = {}, {}
    for day in days:
        key = original["contexts"][day]
        root = study / "receipts" / key
        packet = load(root / "context.json")
        if packet["trade_date"] != day or not matches_archived_identity(packet, model, key):
            raise ValueError("冻结的任务上下文不匹配")
        receipts[day] = read_receipt(root / "decision.json", packet, model, key)
        packets[day] = packet
    report = evaluation(
        study,
        days,
        packets,
        receipts,
        market,
        manifest,
        model,
        {},
        [d for d in days if not packets[d]["documents"]],
    )
    print("已复核原始回执并重新结算", report["evaluated_days"], "个交易日")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument(
        "--settle-frozen", action="store_true", help="仅用完整的冻结模型回执重新结算"
    )
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--model", default="gpt-6.2-sol")
    parser.add_argument("--cli", default=shutil.which("codex"))
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=3)
    args = parser.parse_args()
    if not args.settle_frozen and not all(
        (args.cli, args.start, args.end, args.sources)
    ):
        parser.error("运行模型需要 Codex CLI、起止日期与来源档案")
    with (args.study / "replay.lock").open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.settle_frozen:
            settle_frozen(args.study)
        else:
            run(args)


if __name__ == "__main__":
    main()
