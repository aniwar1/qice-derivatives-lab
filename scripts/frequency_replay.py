"""冻结新协议，重做逐日独立 Agent 排序，并用前一收盘的组合状态执行。"""

import fcntl
import gzip
import json
import shutil
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from app.agent_replay import GROUPS, digest
from app.agent_replay_account import Account, position_value
from app.frequency_account import account_state, prefer, segment, select_order
from app.frequency_model import infer
from app.frequency_policy import POLICY, PROTOCOL, schema, signal_input

from scripts.agent_replay import atomic, load, matches_archived_identity

ROOT = Path(__file__).resolve().parents[1]


def research_day(day, source, output, source_key, model, cli, contract, only_settle):
    packet = load(source / "receipts" / source_key / "context.json")
    if packet["trade_date"] != day or not matches_archived_identity(packet, "gpt-6.2-sol", source_key):
        raise ValueError("原始冻结上下文摘要错误")
    model_input = signal_input(packet)
    prompt = (
        POLICY
        + "\n"
        + json.dumps(model_input, ensure_ascii=False, separators=(",", ":"))
    )
    prompt_hash = sha256(prompt.encode()).hexdigest()
    key = digest([source_key, contract, prompt_hash, model])
    path = output / "rank-receipts" / key / "receipt.json"
    if path.exists():
        receipt = load(path)
        if (
            receipt["runtime"]["prompt_sha256"] != prompt_hash
            or receipt["source_key"] != source_key
            or receipt["key"] != key
            or receipt["runtime"]["model"] != model
            or receipt["rankings_sha256"] != digest(receipt["rankings"])
            or not receipt["runtime"]["run_id"]
        ):
            raise ValueError("排序回执摘要无效")
        return packet, receipt
    if only_settle:
        raise RuntimeError("尚缺该日冻结排序回执")
    last_error = None
    for attempt in range(2):
        try:
            rankings, runtime = infer(model, cli, prompt, schema(packet["products"]))
            if set(rankings) != set(GROUPS) or any(
                set(pair) != {"primary", "reserve"} for pair in rankings.values()
            ):
                raise ValueError("模型缺少完整排序")
            receipt = {
                "day": day,
                "cutoff": packet["cutoff"],
                "source_key": source_key,
                "key": key,
                "rankings": rankings,
                "rankings_sha256": digest(rankings),
                "runtime": runtime,
                "attempt": attempt + 1,
            }
            atomic(path, receipt)
            return packet, receipt
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            last_error = exc
    raise RuntimeError(str(last_error))


def asof_nav(account, market, history, metadata, cutoff, universe):
    value = account.cash
    for pos in account.positions:
        exchange = universe[pos["product"]]
        for day in reversed(history):
            if day < pos["day"]:
                break
            source = metadata.get((day, exchange, "option"))
            if not source or (
                source.get("disclosed_available_at")
                and datetime.fromisoformat(source["disclosed_available_at"]) > cutoff
            ):
                continue
            rows = [market[day]["option"].get(leg) for leg in pos["legs"]]
            if all(r and r[3] and r[3] > 0 and r[4] and r[4] > 0 for r in rows):
                value += position_value(pos, [r[3] for r in rows])
                break
        else:
            day = None
        if day is None or day < pos["day"]:
            value += pos["cost"]
    return value


def build_report(source, days, rows, market, metadata, model, contract, expiry_path):
    from akshare.futures.cons import get_calendar
    from app.option_expiry import historical_expiry

    from scripts.expiry_settlement import ExpiryLedger

    base = load(source / "results.json")
    universe = base["products"]
    before = {d: days[:n] for n, d in enumerate(days)}
    exchanges = {p: v["exchange"] for p, v in rows[days[0]][0]["products"].items()}
    calendar = sorted(date.fromisoformat(value).isoformat() for value in get_calendar())
    contracts = {
        product["technical"]["contract"]
        for packet, _ in rows.values()
        for product in packet["products"].values()
        if product["options"] and product["technical"]
    }
    expiries = {
        contract_id: expiry.isoformat()
        for contract_id in sorted(contracts)
        if (expiry := historical_expiry(contract_id, calendar)) is not None
    }
    expiry_ledger = ExpiryLedger(
        source, expiry_path.parent, load(source / "market/source-manifest.json")
    )
    index = expiry_ledger.index
    observed = {}
    for day in days:
        packet, receipt = rows[day]
        cutoff = datetime.fromisoformat(packet["cutoff"])
        symbols = {
            leg
            for pair in receipt["rankings"].values()
            for plan in pair.values()
            if plan
            for leg in plan["legs"]
        }
        observed[day] = {
            leg: record
            for leg in sorted(symbols)
            if (record := index.lookup(leg, cutoff))
        }
    variants = []
    for variant, depth in PROTOCOL["variants"].items():
        plans = []
        for group in GROUPS:
            ledger = Account(group)
            curve, sessions = [], []
            for day in days:
                packet, receipt = rows[day]
                day_index = calendar.index(day)
                packet = packet | {
                    "execution_expiries": expiries,
                    "execution_leg_expiries": observed[day],
                    "execution_sessions": calendar[day_index : day_index + 6],
                }
                cutoff = datetime.fromisoformat(packet["cutoff"])
                ledger.previous_equity = asof_nav(
                    ledger, market, before[day], metadata, cutoff, exchanges
                )
                state = account_state(ledger)
                choice, rank, trace = select_order(
                    group, receipt["rankings"][group], packet, ledger, depth
                )
                delayed = {
                    exchange
                    for exchange in set(exchanges.values())
                    if (
                        stamp := metadata.get((day, exchange, "future"), {}).get(
                            "disclosed_available_at"
                        )
                    )
                    and datetime.fromisoformat(stamp)
                    > datetime.fromisoformat(day + "T20:00:00+08:00")
                }
                result, point = expiry_ledger.process(
                    ledger, day, choice, packet, market[day], delayed
                )
                curve.append(point)
                sessions.append(
                    {
                        "day": day,
                        "cutoff": packet["cutoff"],
                        "receipt_key": receipt["key"],
                        "source_key": receipt["source_key"],
                        "account_before": state,
                        "rankings": receipt["rankings"][group],
                        "selected_rank": rank,
                        "screen": trace,
                        "decision": choice,
                        "execution": result,
                    }
                )
            stats = segment(curve, sessions, 0, len(days)) | {
                "realized": curve[-1]["realized"],
                "open_pnl": curve[-1]["open_pnl"],
                "open_positions": curve[-1]["open_positions"],
                "reserve_orders": sum(s["selected_rank"] == 2 for s in sessions),
                "reserve_fills": sum(
                    s["selected_rank"] == 2
                    and s["execution"]["status"] == "simulated_open"
                    for s in sessions
                ),
                "strict_verified_fills": 0,
            }
            plans.append(
                {
                    "id": group,
                    "stats": stats,
                    "curve": curve,
                    "sessions": sessions,
                    "segments": {
                        "first83": segment(curve, sessions, 0, 83),
                        "last42": segment(curve, sessions, 83, 125),
                    },
                }
            )
        variants.append({"id": variant, "plans": plans})
    comparisons = []
    for index, g in enumerate(GROUPS):
        old = next(p for p in base["plans"] if p["id"] == g)
        current = {
            "stats": segment(old["curve"], old["sessions"], 0, 125),
            "segments": {"last42": segment(old["curve"], old["sessions"], 83, 125)},
        }
        first, expanded = (v["plans"][index] for v in variants)
        preferred = "ranked" if prefer(expanded, first) else "primary"
        candidate = expanded if preferred == "ranked" else first
        comparisons.append(
            {
                "group": g,
                "preferred": preferred,
                "passes_vs_previous": prefer(candidate, current),
                "previous": current,
            }
        )
    return {
        "status": "complete",
        "planned_days": len(days),
        "completed_days": len(rows),
        "evaluated_days": len(days),
        "products": universe,
        "start": days[0],
        "end": days[-1],
        "model": model,
        "protocol": PROTOCOL,
        "protocol_sha256": contract,
        "settlement_revision": "qualified-account-v2",
        "settlement_rules": "备选只用于首选本身证据、流动性与可核实期限合格，且真实账户持仓或风险预算限制首选的情况。开盘受阻不改选。期限按事前公布规则及交易日历计算。",
        "expiry_calendar_sha256": digest(calendar),
        "expiry_index_sha256": sha256(expiry_path.read_bytes()).hexdigest(),
        "verified_expiries": expiries,
        "missing_expiry_contracts": sorted(contracts - set(expiries)),
        "variants": variants,
        "comparisons": comparisons,
        "receipts": {d: rows[d][1]["key"] for d in days},
    }


def run(args):
    from scripts.expiry_settlement import ABANDON_HASH

    source, output = args.source.resolve(), args.output.resolve()
    manifest = load(source / "run-manifest.json")
    market_manifest = load(source / "market/source-manifest.json")
    raw = (source / "market/market.json.gz").read_bytes()
    if (
        sha256(raw).hexdigest() != manifest["market_sha256"]
        or digest(market_manifest) != manifest["market_manifest_sha256"]
    ):
        raise ValueError("冻结行情版本不匹配")
    days = manifest["calendar"]
    if len(days) != 125 or len(set(days)) != 125 or days != sorted(days):
        raise ValueError("本实验要求125个有序交易日")
    modules = [
        "backend/app/frequency_policy.py",
        "backend/app/frequency_account.py",
        "backend/app/frequency_model.py",
        "backend/app/agent_replay.py",
        "backend/app/agent_replay_account.py",
        "scripts/frequency_replay.py",
    ]
    registration = {
        "protocol": PROTOCOL,
        "policy": POLICY,
        "model": args.model,
        "modules": {p: sha256((ROOT / p).read_bytes()).hexdigest() for p in modules},
        "baseline_inputs": manifest["contexts"],
        "schema": schema(market_manifest["configured_universe"]),
    }
    lock = output / "ranking-policy.json"
    if lock.exists():
        frozen = load(lock)
        # 账户与结算可以纠错，模型、提示、资料和排名传输代码必须完全一致。
        model_files = (
            "backend/app/frequency_policy.py",
            "backend/app/frequency_model.py",
            "backend/app/agent_replay.py",
            "backend/app/agent_replay_account.py",
        )
        if any(
            frozen[k] != registration[k]
            for k in ("protocol", "policy", "model", "schema", "baseline_inputs")
        ) or any(
            frozen["modules"][p] != registration["modules"][p] for p in model_files
        ):
            raise ValueError("模型、排序协议或冻结资料改变，不能混用模型回执")
        registration = frozen
    else:
        if args.settle_frozen:
            raise ValueError("缺少原始排序协议，不能只读复算")
        atomic(lock, registration)
    contract = digest(registration)
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {
            pool.submit(
                research_day,
                day,
                source,
                output,
                manifest["contexts"][day],
                args.model,
                args.cli,
                contract,
                args.settle_frozen,
            ): day
            for day in days
        }
        for job in as_completed(jobs):
            day = jobs[job]
            try:
                results[day] = job.result()
                print(day, len(results), "/", len(days), flush=True)
            except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
                errors[day] = str(exc)
                print(day, "error", str(exc), flush=True)
            if not args.settle_frozen:
                atomic(
                    output / "ranking-progress.json",
                    {
                        "status": "running",
                        "planned_days": len(days),
                        "completed_days": len(results),
                        "errors": errors,
                    },
                )
    if errors:
        raise SystemExit(1)
    ids = {r[1]["runtime"]["run_id"] for r in results.values()}
    if len(ids) != len(days):
        raise ValueError("排序回执具有重复模型运行标识")
    expiry_path = output / "settlement-inputs" / "expiry-index.json"
    if not expiry_path.exists():
        atomic(expiry_path, load(output / "expiry-index.json"))
    rule = Path("expiry-rules") / (ABANDON_HASH + ".pdf")
    snapshot_rule = expiry_path.parent / rule
    snapshot_rule.parent.mkdir(exist_ok=True)
    if not snapshot_rule.exists():
        tmp_rule = snapshot_rule.with_suffix(".tmp")
        shutil.copyfile(output / rule, tmp_rule)
        if sha256(tmp_rule.read_bytes()).hexdigest() != ABANDON_HASH:
            raise ValueError("待冻结的到期行权规则原件摘要不一致")
        tmp_rule.replace(snapshot_rule)
    for original in load(expiry_path)["sources"]:
        stored = output / "expiry-originals" / original["raw"]
        if (
            sha256(gzip.decompress(stored.read_bytes())).hexdigest()
            != original["sha256"]
        ):
            raise ValueError("逐腿到期原件摘要不符")
    report = build_report(
        source,
        days,
        results,
        json.loads(gzip.decompress(raw)),
        {(r["day"], r["exchange"], r["type"]): r for r in market_manifest["sources"]},
        args.model,
        contract,
        expiry_path,
    )
    previous_path = output / "replay.json"
    report["settlement_modules"] = {
        p: sha256((ROOT / p).read_bytes()).hexdigest()
        for p in (
            "backend/app/frequency_account.py",
            "backend/app/option_expiry.py",
            "scripts/expiry_settlement.py",
            "backend/app/agent_replay_account.py",
            "scripts/frequency_replay.py",
        )
    }
    if previous_path.is_file():
        previous = load(previous_path)
        if (
            previous.get("settlement_revision") != report["settlement_revision"]
            or previous.get("settlement_modules") != report["settlement_modules"]
        ):
            history = output / "settlement-versions"
            history.mkdir(exist_ok=True)
            archived = history / (
                sha256(previous_path.read_bytes()).hexdigest() + ".json"
            )
            if not archived.exists():
                shutil.copy2(previous_path, archived)
    atomic(previous_path, report)
    atomic(
        output / "ranking-progress.json",
        {
            "status": "complete",
            "completed_days": len(results),
            "planned_days": len(days),
            "errors": {},
        },
    )


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="gpt-6.2-sol")
    parser.add_argument("--workers", type=int, default=8, choices=range(1, 9))
    parser.add_argument("--cli", default=shutil.which("codex"))
    parser.add_argument("--settle-frozen", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "ranking.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args)
