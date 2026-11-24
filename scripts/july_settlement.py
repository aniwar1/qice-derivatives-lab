"""以七月初空仓账户、冻结策略回执和交易所行情重算三个固定预算组合。"""

import argparse
import fcntl
import gzip
import json
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from app.agent_replay import BJT, GROUPS, digest
from app.agent_replay_account import (
    FIXED_STRATEGY_VERSION,
    FIXED_TRADE_BUDGET,
    SETTLEMENT_REVISION,
    INITIAL,
    Account,
)
from app.execution_research import ExecutionAccount, select_order
from app.models import PRODUCT_EXCHANGES
from app.second_entry import SecondEntryAccount
from app.rolling_calendar import trading_days
from app.rolling_marks import finish_marks
from app.strategy_overview import START, read_receipt
from app.study_view import BATCH

from scripts.agent_replay import atomic, load
from scripts.collect_replay_market import parse
from scripts.frequency_replay import asof_nav


def _forward_day(folder, day, receipt_path):
    settlement = load(receipt_path)
    if settlement["day"] != day or settlement["sha256"] != digest(
        {k: v for k, v in settlement.items() if k != "sha256"}
    ):
        raise ValueError(f"{day} 行情归档摘要不一致")
    prices = {"future": {}, "option": {}}
    for source in settlement["market_sources"]:
        raw = gzip.decompress((folder / "market" / source["raw"]).read_bytes())
        if source["day"] != day or sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError(f"{day} 交易所原件日期或摘要不符")
        rows, _ = parse(source["exchange"], source["type"], day, raw, PRODUCT_EXCHANGES)
        prices[source["type"]].update(rows)
    if not prices["future"] or not prices["option"]:
        raise ValueError(f"{day} 缺少可核实的期货或期权日行情")
    return settlement, prices


def _settle_one(
    root, day, group, account, record, prices, sources, market_dir, delayed
):
    decision = record["decisions"][group] if record else None
    packet = (
        record["packet"]
        if record
        else {
            "products": {
                p: {"exchange": exchange} for p, exchange in PRODUCT_EXCHANGES.items()
            }
        }
    )
    variant = getattr(account, "execution_variant", "opening")
    source = group
    if record and variant != "opening":
        decision, source = select_order(group, record["decisions"], packet, account.positions, variant)
    product_set = {p["product"] for p in account.positions}
    if decision and decision["action"] == "enter":
        product_set.add(decision["product"])
    for product in product_set:
        exchange = PRODUCT_EXCHANGES[product]
        kinds = {s["type"] for s in sources if s["exchange"] == exchange}
        if not {"future", "option"} <= kinds:
            raise ValueError(f"{day} {product} 缺少期货或期权交易所日行情")
    waiting = {
        "action": "wait",
        "thesis": "当日缺少盘前封存判断，仅依既定条件管理在手仓位",
    }
    order = decision or waiting
    prior_peak, prior_drawdown = account.peak, account.drawdown
    execution, point = account.process(day, order, packet, prices, delayed)
    if decision is None:
        execution["status"] = "no_sealed_signal"
    if execution["status"] == "simulated_open":
        if variant in {"second_signal", "confirmed_second"}:
            execution["same_product_entry_dates"] = [p["day"] for p in account.positions if p["product"] == decision["product"]]
        cost = execution["premium_and_fees"]
        budget = execution["budget_cny"]
        if not 0 < cost <= budget <= FIXED_TRADE_BUDGET:
            raise ValueError(f"{day} {group} 新开仓成本超预算")
    finish_marks(
        root,
        day,
        account,
        execution,
        point,
        packet,
        prices,
        sources,
        prior_peak,
        prior_drawdown,
        market_dir=market_dir,
    )
    if abs(INITIAL + point["realized"] + point["open_pnl"] - point["equity"]) > 0.02:
        raise ValueError(f"{day} {group} 账户净值无法核对")
    session = {
        "day": day,
        "decision": decision,
        "execution": execution,
        "cutoff": record["cutoff"] if record else None,
        "decision_sha256": record["decision_sha256"] if record else None,
        "mode": record["mode"] if record else "missed_preopen",
    }
    if variant != "opening":
        session.update(execution_variant=variant, order_source=source,
                       original_decision=record["decisions"][group] if record else None)
    return point, session


def selected_policy(root):
    path = root / "config/selected_execution.json"
    if not path.is_file():
        path = Path(__file__).resolve().parents[1] / "config/selected_execution.json"
    return load(path)["groups"]


def build(root, *, candidate=None, policy_by_group=None, stop_at=None):
    folder = root / "local-data/july-strategy"
    historical = root / "local-data/agent-study" / BATCH
    forward = root / "local-data/rolling-six"
    calendar = trading_days(root)
    if policy_by_group is None:
        policy_by_group = selected_policy(root)
    if set(policy_by_group) != set(GROUPS) or set(policy_by_group.values()) - {"opening", "day_limit", "short_rotation", "second_signal", "confirmed_second"}:
        raise ValueError("未识别的组合执行规则")
    accounts = {}
    for group, name in policy_by_group.items():
        if name == "opening":
            accounts[group] = Account(group)
        elif name in {"second_signal", "confirmed_second"}:
            accounts[group] = SecondEntryAccount(group, name)
        else:
            accounts[group] = ExecutionAccount(group, name)
    groups = {
        g: {"sessions": [], "points": [], "opened": 0, "closed": 0} for g in GROUPS
    }
    issues = []
    ledger = {
        "version": FIXED_STRATEGY_VERSION,
        "start": START,
        "settlement_revision": SETTLEMENT_REVISION,
        "end": None,
        "groups": groups,
        "execution_policy": policy_by_group,
        "initial_capital": INITIAL,
        "trade_budget": FIXED_TRADE_BUDGET,
        "issues": issues,
    }
    manifest = load(historical / "market/source-manifest.json")
    reference = load(historical / "run-manifest.json")
    if (
        digest(manifest) != reference["market_manifest_sha256"]
        or manifest["data_sha256"] != reference["market_sha256"]
    ):
        raise ValueError("历史行情档案与封存研究批次的摘要不符")
    sources_by_day = defaultdict(list)
    for s in manifest["sources"]:
        sources_by_day[s["day"]].append(s)
    metadata = {(s["day"], s["exchange"], s["type"]): s for s in manifest["sources"]}
    archive = historical / "market/market.json.gz"
    raw = archive.read_bytes()
    if sha256(raw).hexdigest() != manifest["data_sha256"]:
        raise ValueError("研究期合约日线与交易所行情清单摘要不符")
    market = json.loads(gzip.decompress(raw))
    prior = []
    previous_market_hash = None
    last_historical = max(market)
    now = datetime.now(BJT)
    daily_files = {
        p.stem: p
        for p in (forward / "settlements").glob("*.json")
        if p.stem <= "2026-11-24"
    }
    daily_files.update({p.stem: p for p in (folder / "market-days").glob("*.json")})
    latest = max(last_historical, max(daily_files, default=last_historical))
    if candidate is not None:
        proposed_day, proposed_path = candidate
        index = calendar.index(latest) + 1
        if (
            index >= len(calendar)
            or proposed_day != calendar[index]
            or proposed_day > now.date().isoformat()
        ):
            raise ValueError("待封存行情必须对应最近日结的下一交易日且已经到达该交易日")
        daily_files[proposed_day] = proposed_path
        latest = proposed_day
    for day in calendar:
        if stop_at and day > stop_at:
            break
        if day < START or day > min(now.date().isoformat(), latest):
            continue
        receipt_path = folder / "decisions-v2" / (day + ".json")
        record = read_receipt(receipt_path) if receipt_path.is_file() else None
        if day <= last_historical:
            sources = sources_by_day[day]
            market_dir = historical / "market"
            missed = False
        else:
            if day not in daily_files:
                issues.append(f"{day} 尚无日结行情，曲线截至上一笔可核实日结。")
                break
            saved, market[day] = _forward_day(forward, day, daily_files[day])
            if (
                previous_market_hash is not None
                and saved["prev_sha256"] != previous_market_hash
            ):
                raise ValueError(f"{day} 日行情档案与前一交易日的摘要不连续")
            previous_market_hash = saved["sha256"]
            if daily_files[day].parent.name == "market-days" or (
                candidate and day == candidate[0]
            ):
                if record:
                    if (
                        saved["decision_status"] != "sealed_preopen"
                        or record["mode"] != "preopen"
                        or saved["decision_sha256"] != record["decision_sha256"]
                    ):
                        raise ValueError(f"{day} 日结与盘前封存的委托不符")
                elif saved["decision_status"] != "missed_preopen" or saved.get(
                    "decision_sha256"
                ):
                    raise ValueError(f"{day} 日结缺少对应的策略回执")
            # 前向账户只能在实际取得原件之后使用该版行情作交易判断。
            sources = []
            for original in saved["market_sources"]:
                observed = datetime.fromisoformat(original["retrieved_at"])
                released = datetime.fromisoformat(
                    original.get("disclosed_available_at") or original["retrieved_at"]
                )
                sources.append(
                    original
                    | {"disclosed_available_at": max(observed, released).isoformat()}
                )
            market_dir = forward / "market"
            metadata.update({(day, s["exchange"], s["type"]): s for s in sources})
            missed = saved["decision_status"] == "missed_preopen"
        if not record and not missed:
            issues.append(f"{day} 的新版策略回执尚未完成，此日及其后暂不计入收益曲线。")
            break
        if not record:
            issues.append(f"{day} 缺少盘前判断，按在手仓位原有条件管理，当日未新开仓。")
        threshold = datetime.fromisoformat(day + "T20:00:00+08:00")
        delayed = {
            s["exchange"]
            for s in sources
            if s["type"] == "future"
            and s.get("disclosed_available_at")
            and datetime.fromisoformat(s["disclosed_available_at"]) > threshold
        }
        settled = {}
        for group, account in accounts.items():
            cutoff = datetime.fromisoformat(record["cutoff"]) if record else None
            if (
                cutoff
                and prior
                and any(
                    (
                        released := metadata.get(
                            (prior[-1], PRODUCT_EXCHANGES[p["product"]], "option"), {}
                        ).get("disclosed_available_at")
                    )
                    and datetime.fromisoformat(released) > cutoff
                    for p in account.positions
                )
            ):
                account.previous_equity = asof_nav(
                    account, market, prior, metadata, cutoff, PRODUCT_EXCHANGES
                )
            try:
                settled[group] = _settle_one(
                    root,
                    day,
                    group,
                    account,
                    record,
                    market[day],
                    sources,
                    market_dir,
                    delayed,
                )
            except (ValueError, LookupError) as exc:
                issues.append(f"{day} {group} 的收盘待核实：{exc}")
                break
        if len(settled) != len(GROUPS):
            break
        for group, (point, session) in settled.items():
            account = accounts[group]
            groups[group]["points"].append(point)
            groups[group]["sessions"].append(session)
            groups[group].update(
                opened=account.opened,
                closed=account.closed,
                account=deepcopy(vars(account)),
            )
        missing = []
        for exchange in sorted(set(PRODUCT_EXCHANGES.values())):
            present = {s["type"] for s in sources if s["exchange"] == exchange}
            kinds = [
                name
                for key, name in (("future", "期货"), ("option", "期权"))
                if key not in present
            ]
            if kinds:
                missing.append(f"{exchange} {'、'.join(kinds)}")
        ledger.update(end=day, missing_market=missing)
        prior.append(day)
    expected = [
        day
        for day in calendar
        if START <= day < now.date().isoformat()
        or (day == now.date().isoformat() and now.hour >= 16)
    ]
    if expected and latest < expected[-1]:
        issues.append(
            f"最新应结算交易日 {expected[-1]}，交易所日行情档案截至 {latest}。"
        )
    if ledger.get("missing_market"):
        issues.append(
            f"{ledger['end']} 日行情缺少{'；'.join(ledger['missing_market'])}原表。"
        )
    return ledger


def refresh(workspace):
    folder = workspace / "local-data/july-strategy"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "settlement-v2.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        destination = folder / "settlement-v2.json"
        previous = load(destination) if destination.exists() else None
        if previous and (previous["version"] != FIXED_STRATEGY_VERSION
                         or previous.get("settlement_revision") != SETTLEMENT_REVISION
                         or previous.get("execution_policy") != selected_policy(workspace)):
            previous = None
        try:
            report = build(workspace)
        except (OSError, KeyError, ValueError, TypeError, LookupError) as error:
            if not previous:
                raise
            report = {
                **previous,
                "issues": [f"本次重算待核实，保留上次完成的日结：{error}"],
            }
        if previous and (report["end"] or "") < (previous["end"] or ""):
            report = {
                **previous,
                "issues": report["issues"] + ["保留上次通过核对的完整曲线。"],
            }
        atomic(destination, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    result = refresh(parser.parse_args().workspace)
    print(
        json.dumps(
            {k: result[k] for k in ("start", "end", "issues")}, ensure_ascii=False
        )
    )
