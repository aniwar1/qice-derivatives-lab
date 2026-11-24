"""继承已核实的三套纸面账户，为后续交易日封存盘前判断并按日结账。"""

from __future__ import annotations

import fcntl
import gzip
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.agent_replay import atomic, matches_archived_identity, load, read_receipt
from scripts.collect_replay_market import collect, parse

from .agent_replay import BJT, GROUPS, build_packet, digest, validate_decision
from .agent_replay_account import Account
from .agent_replay_model import decide
from .agent_sources import collect_agent_sources, lookup_agent_sources
from .config import _default_codex_bin
from .models import PRODUCT_EXCHANGES
from .rolling_calendar import trading_days
from .rolling_marks import finish_marks, inherited_expiries
from .rolling_monthly import monthly_context
from .study_view import BATCH


class DataGap(ValueError):
    """仍需取得或核对的真实材料。保留原账户，下一轮继续。"""


def _now():
    return datetime.now(BJT)


def _folder(root):
    return root / "local-data/rolling-six"


def _base(root):
    return root / "local-data/agent-study" / BATCH


def _time(day, hour):
    return datetime.combine(date.fromisoformat(day), time(hour), BJT)


def _hash(raw):
    return sha256(raw).hexdigest()


def _read(path):
    obj = load(path)
    expected = obj.pop("sha256")
    if expected != digest(obj):
        raise ValueError("滚动档案摘要不一致")
    return obj | {"sha256": expected}


def _seal(path, record, *, before=None):
    if path.exists():
        old = _read(path)
        if old["sha256"] != digest(record):
            raise ValueError("已封存的日结或决策不能覆盖")
        return old
    saved = record | {"sha256": digest(record)}
    # 单生产者文件锁与同目录原子替换保证读者只能看到完整对象。
    if before is None:
        atomic(path, saved)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=path.parent) as tmp:
            prepared = Path(tmp) / "record"
            atomic(prepared, saved)
            # 摘要和正文已刷盘，再验证时钟并用不可覆盖的原子链接对外发布。
            if _now() >= before:
                raise DataGap("写入前已超过封存截止时刻")
            os.link(prepared, path)
            if _now() >= before:
                path.unlink()
                raise DataGap("封存跨过截止时刻，作废本次记录")
    return saved


def latest_account_snapshot(workspace_root: Path) -> dict | None:
    folder = _folder(workspace_root)
    if not (folder / "seed.json").is_file():
        return None
    last = _read(folder / "seed.json")
    cal = trading_days(workspace_root)
    idx = cal.index(last["day"])
    for path in sorted((folder / "settlements").glob("*.json")):
        obj = _read(path)
        idx += 1
        if (
            idx >= len(cal)
            or path.stem != cal[idx]
            or obj["day"] != path.stem
            or obj["prev_sha256"] != last["sha256"]
        ):
            raise ValueError("账户日结日期或承接摘要不连续")
        last = obj
    return last


def _seed_record(root):
    base = _base(root)
    original = load(base / "run-manifest.json")
    report = load(base / "results.json")
    manifest = load(base / "market/source-manifest.json")
    raw = (base / "market/market.json.gz").read_bytes()
    days = original["calendar"]
    if (
        report["status"] != "complete"
        or len(days) != report["evaluated_days"]
        or len(days) != 125
        or list(days) != sorted(set(days))
        or set(original["contexts"]) != set(days)
        or digest(manifest) != original["market_manifest_sha256"]
        or _hash(raw) != original["market_sha256"]
        or manifest["data_sha256"] != original["market_sha256"]
        or report["market_sha256"] != original["market_sha256"]
    ):
        raise DataGap("基线未完整结算或来源校验失败")
    market = json.loads(gzip.decompress(raw))
    accounts = {g: Account(g) for g in GROUPS}
    plans = {p["id"]: p for p in report["plans"]}
    if set(plans) != set(GROUPS):
        raise DataGap("基线策略组不完整")
    delayed = {}
    for r in manifest["sources"]:
        if (
            r["type"] == "future"
            and r.get("disclosed_available_at")
            and datetime.fromisoformat(r["disclosed_available_at"])
            > _time(r["day"], 20)
        ):
            delayed.setdefault(r["day"], set()).add(r["exchange"])
    for i, day in enumerate(days):
        key = original["contexts"][day]
        folder = base / "receipts" / key
        packet = load(folder / "context.json")
        if packet["trade_date"] != day or not matches_archived_identity(packet, original["model"], key):
            raise ValueError("基线模型输入未通过校验")
        receipt = read_receipt(folder / "decision.json", packet, original["model"], key)
        for g, account in accounts.items():
            order, point = account.process(
                day, receipt["decisions"][g], packet, market[day], delayed.get(day, ())
            )
            saved = plans[g]["curve"][i]
            if (
                point != saved
                or plans[g]["sessions"][i]["decision"] != receipt["decisions"][g]
                or plans[g]["sessions"][i]["execution"] != order
            ):
                raise ValueError(f"{day} {g} 原始交易、仓位或权益不一致，不能接账")
    end = days[-1]
    inherited_expiries(root, base, accounts, _time(end, 20))
    groups = {
        g: {"variant": "baseline", "account": vars(a), "point": plans[g]["curve"][-1]}
        for g, a in accounts.items()
    }
    return {
        "version": 1,
        "day": end,
        "baseline_end": end,
        "recorded_at": _now().isoformat(),
        "source_sha256": {
            "results": _hash((base / "results.json").read_bytes()),
            "market": _hash(raw),
            "manifest": _hash((base / "run-manifest.json").read_bytes()),
        },
        "groups": groups,
    }


def _seed(root):
    return _seal(_folder(root) / "seed.json", _seed_record(root))


def _upgrade_seed(root, old):
    """已有引用保持原链。尚无引用的旧种子经完整复算后追加逐腿期限。"""
    folder = _folder(root)
    if old["day"] != old["baseline_end"] or not any(
        "expiry_legs" not in pos
        for g in old["groups"].values()
        for pos in g["account"]["positions"]
    ):
        return old
    if any(
        any((folder / k).rglob("*.json"))
        for k in ("settlements", "decisions", "inputs", "staged")
    ):
        return old
    record = _seed_record(root)
    record["recorded_at"] = old["recorded_at"]
    check = json.loads(json.dumps(record))
    for g in check["groups"].values():
        for pos in g["account"]["positions"]:
            pos.pop("expiry_legs", None)
    if digest(check) != old["sha256"] or _read(folder / "seed.json") != old:
        raise ValueError("旧版期初账本与全量重算不符，未升级")
    _seal(folder / "seed-versions" / (old["sha256"] + ".json"), check)
    record.update(previous_seed_sha256=old["sha256"], upgraded_at=_now().isoformat())
    atomic(folder / "seed.json", record | {"sha256": digest(record)})
    return _read(folder / "seed.json")


def _market(root, day, *, refresh_exchanges=(), frozen_sources=()):
    """每类日行情仅从已校验的交易所原件恢复，缺失接口独立重试。"""
    archive = _folder(root) / "market"
    universe = PRODUCT_EXCHANGES
    frozen = {(s["exchange"], s["type"]): s for s in frozen_sources}
    jobs = [
        (ex, k) for ex in sorted(set(universe.values())) for k in ("future", "option")
    ]

    def fetch(job):
        ex, kind = job
        try:
            if job in frozen:
                r = frozen[job]
            elif ex in refresh_exchanges:
                r = collect(archive, universe, day, ex, kind, reobserve=True)
            else:
                r = collect(archive, universe, day, ex, kind)
            if r["day"] != day or r["exchange"] != ex or r["type"] != kind:
                raise ValueError("原件身份错误")
            blob = gzip.decompress((archive / r["raw"]).read_bytes())
            if _hash(blob) != r["sha256"]:
                raise ValueError("日行情摘要不一致")
            observed = datetime.fromisoformat(r["retrieved_at"])
            available = datetime.fromisoformat(
                r.get("disclosed_available_at") or r["retrieved_at"]
            )
            if (
                min(observed, available) < _time(day, 0)
                or max(observed, available) > _now()
            ):
                raise ValueError("行情版本或实际取得时间超出边界")
            rows, _ = parse(ex, kind, day, blob, universe)
            return r, rows
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return {
                "day": day,
                "exchange": ex,
                "type": kind,
                "error": type(exc).__name__,
            }, {}

    prices = {"future": {}, "option": {}}
    sources, gaps = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for receipt, rows in pool.map(fetch, jobs):
            if not rows:
                gaps.append(receipt)
            else:
                sources.append(receipt)
                prices[receipt["type"]].update(rows)
    return prices, sources, gaps


def _settle(root, last, day):
    folder = _folder(root)
    prices, sources, gaps = _market(root, day)
    if not prices["future"] or not prices["option"]:
        raise DataGap("本交易日缺少可核实的期货或期权收盘原件")
    decision_path = folder / "decisions" / f"{day}.json"
    saved = _read(decision_path) if decision_path.exists() else None
    if saved:
        packet = saved["packet"]
        if (
            saved["prev_sha256"] != last["sha256"]
            or saved["day"] != day
            or saved["context_sha256"] != digest(packet)
            or packet["previous_account_sha256"] != last["sha256"]
            or packet["trade_date"] != day
            or not _time(last["day"], 20)
            <= datetime.fromisoformat(saved["recorded_at"])
            < _time(last["day"], 21)
        ):
            raise ValueError("盘前决策的时点或账本与日结不符")
        validate_decision(saved["decisions"], packet)
    else:
        packet = {
            "products": {p: {"exchange": ex} for p, ex in PRODUCT_EXCHANGES.items()}
        }
    have = {(r["exchange"], r["type"]) for r in sources}
    delayed = {
        r["exchange"]
        for r in sources
        if r["type"] == "future"
        and max(
            datetime.fromisoformat(r[k])
            for k in ("retrieved_at", "disclosed_available_at")
            if r.get(k)
        )
        > _time(day, 20)
    }
    groups = {}
    for group in GROUPS:
        account = Account(group)
        account.__dict__.update(
            json.loads(json.dumps(last["groups"][group]["account"]))
        )
        if any(
            "expiry_legs" not in p and p["day"] <= last["baseline_end"]
            for p in account.positions
        ):
            inherited_expiries(
                root, _base(root), {group: account}, _time(last["baseline_end"], 20)
            )
        decision = saved["decisions"][group] if saved else None
        active = {p["product"] for p in account.positions}
        if decision and decision["action"] == "enter":
            active.add(decision["product"])
        for product in active:
            if any(
                (PRODUCT_EXCHANGES[product], k) not in have
                for k in ("future", "option")
            ):
                raise DataGap(f"{day} {product} 缺少持仓或候选所属交易所的日行情")
        # 停机期间没有盘前决策，仅执行已有持仓事先确定的退出规则。
        standing = decision or {"action": "wait", "thesis": "没有盘前封存的新建仓判断"}
        prior_peak, prior_drawdown = account.peak, account.drawdown
        order, point = account.process(day, standing, packet, prices, delayed)
        try:
            finish_marks(
                root,
                day,
                account,
                order,
                point,
                packet,
                prices,
                sources,
                prior_peak,
                prior_drawdown,
            )
        except LookupError as exc:
            raise DataGap(str(exc)) from exc
        if not saved:
            order["status"] = "no_sealed_signal"
        groups[group] = {
            "variant": "baseline",
            "point": point,
            "account": vars(account),
            "decision": decision,
            "execution": order,
        }
    recorded = _now()
    return _seal(
        folder / "settlements" / f"{day}.json",
        {
            "version": 1,
            "day": day,
            "baseline_end": last["baseline_end"],
            "prev_sha256": last["sha256"],
            "recorded_at": recorded.isoformat(),
            "settlement_timing": "same_session"
            if recorded.date().isoformat() == day
            else "delayed_reconciliation",
            "latest_market_observed_at": max(
                datetime.fromisoformat(r["retrieved_at"]) for r in sources
            )
            .astimezone(BJT)
            .isoformat(),
            "decision_status": "sealed_preopen" if saved else "missed_preopen",
            "decision_sha256": saved["sha256"] if saved else None,
            "market_sources": sources,
            "market_gaps": gaps,
            "groups": groups,
        },
    )


def _store_blob(folder, checksum, raw):
    if _hash(raw) != checksum:
        raise ValueError("冻结资料原件摘要错误")
    path = folder / "evidence" / checksum
    if path.exists():
        if _hash(path.read_bytes()) != checksum:
            raise ValueError("已存原件发生变化")
    else:
        from scripts.collect_replay_market import atomic as write_bytes

        write_bytes(path, raw)


def _news(root, cutoff):
    archive = root / "local-data/agent-sources"
    products = list(PRODUCT_EXCHANGES)
    observed = collect_agent_sources(products, archive_root=archive)
    cutoff = min(_now(), cutoff)
    packet = lookup_agent_sources(products, archive_root=archive, cutoff=cutoff)
    packet["source_status"] = observed["source_status"]
    looked_up = packet["items"]
    packet["items"] = [
        i
        for i in packet["items"]
        if all(
            datetime.fromisoformat(i[key]) <= cutoff
            for key in ("published_at", "available_at", "retrieved_at")
        )
    ]
    if len(looked_up) != len(packet["items"]):
        gaps = {(g["product"], g["dimension"]) for g in packet.get("gaps", [])}
        for product in products:
            items = [i for i in packet["items"] if product in i.get("products", [])]
            checks = {
                "fundamentals": any(i["dimension"] == "fundamentals" for i in items),
                "news": any(i["dimension"] == "news" for i in items),
                "direct_fundamental": any(
                    i["dimension"] == "fundamentals" and i.get("scope") == "direct"
                    for i in items
                ),
                "published_news": any(
                    i["dimension"] == "news" and not i.get("scheduled_at")
                    for i in items
                ),
            }
            for dimension, found in checks.items():
                if not found and (product, dimension) not in gaps:
                    packet.setdefault("gaps", []).append(
                        {
                            "product": product,
                            "dimension": dimension,
                            "reason": "截至资料冻结时没有已观察并核验的原件",
                        }
                    )
    for item in packet["items"]:
        checksum = item["sha256"]
        if item.get("raw"):
            raw = archive / "supplement" / item["raw"]
        else:
            raw = archive / "blobs" / checksum
        _store_blob(_folder(root), checksum, raw.read_bytes())
        if extra := item.get("disclosure_index_sha256"):
            _store_blob(
                _folder(root),
                extra,
                (archive / "supplement/originals" / (extra + ".json")).read_bytes(),
            )
    return packet


def _history(root, last, prior):
    market, index = {}, {}
    folder = _folder(root)
    seed = _read(folder / "seed.json")
    old = [d for d in prior if d <= seed["day"]]
    if old:
        base = _base(root)
        raw = (base / "market/market.json.gz").read_bytes()
        if _hash(raw) != seed["source_sha256"]["market"]:
            raise ValueError("基线历史行情改变")
        legacy = json.loads(gzip.decompress(raw))
        market.update({d: legacy[d] for d in old})
        manifest = load(base / "market/source-manifest.json")
        base_manifest = load(base / "run-manifest.json")
        if (
            _hash((base / "run-manifest.json").read_bytes())
            != seed["source_sha256"]["manifest"]
            or digest(manifest) != base_manifest["market_manifest_sha256"]
        ):
            raise ValueError("基线来源清单改变")
        for r in manifest["sources"]:
            if r["day"] in old:
                index[r["day"], r["exchange"], r["type"]] = r
    for day in prior:
        if day <= seed["day"]:
            continue
        market_day = root / "local-data/july-strategy/market-days" / f"{day}.json"
        archived = market_day if market_day.exists() else folder / "settlements" / f"{day}.json"
        mark = last if day == last["day"] else _read(archived)
        market[day] = {"future": {}, "option": {}}
        for row in mark["market_sources"]:
            raw = gzip.decompress((folder / "market" / row["raw"]).read_bytes())
            if _hash(raw) != row["sha256"]:
                raise ValueError("日结历史行情原件摘要不符")
            ex, kind = row["exchange"], row["type"]
            data, _ = parse(ex, kind, day, raw, PRODUCT_EXCHANGES)
            market[day][kind].update(data)
            index[day, ex, kind] = row
    return market, index


def _packet(root, last, next_day, calendar):
    from .live_expiry import collect_expiries
    from .market_psychology import NEWS_REVIEW_GUIDANCE, attach_reaction_history

    cutoff = _time(last["day"], 20)
    j = calendar.index(next_day)
    prior = list(calendar[max(0, j - 21) : j])
    market, index = _history(root, last, prior)
    # 收盘结算后才恢复的交易所也可进入研究，此处再次尝试本次交易日各原件。
    if prior[-1] > last["baseline_end"]:
        fixed_archive = root / "local-data/july-strategy/market-days" / f"{prior[-1]}.json"
        if fixed_archive.exists():
            # 已结算的版本继续供盘前研究使用，缺失的交易所仍可补采。
            prices, sources, _ = _market(root, prior[-1], frozen_sources=last["market_sources"])
        else:
            prices, sources, _ = _market(root, prior[-1])
        market[prior[-1]] = prices
        for r in sources:
            index[r["day"], r["exchange"], r["type"]] = r
    # 正文和版本时点均须先于本次资料截止，历史归档也受这个边界约束。
    index = {
        key: r
        for key, r in index.items()
        if r.get("retrieved_at")
        and max(
            datetime.fromisoformat(r[k])
            for k in ("retrieved_at", "disclosed_available_at")
            if r.get(k)
        )
        <= min(_now(), cutoff)
    }
    news = _news(root, cutoff)
    specs = {
        r["symbol"]: r
        for r in load(root / "config/product_contract_specs.json")["products"]
    }
    if set(specs) != set(PRODUCT_EXCHANGES):
        raise ValueError("交易范围与乘数配置不一致")
    monthly, monthly_status = monthly_context(root, cutoff, _now, _store_blob)
    packet = build_packet(
        next_day,
        prior,
        market,
        index,
        PRODUCT_EXCHANGES,
        specs,
        news["items"],
        monthly=monthly,
    )
    attach_reaction_history(packet, prior, market, index)
    if not any(p["technical"] for p in packet["products"].values()):
        raise DataGap("缺少可证实的当日期货日行情，不能形成盘前判断")
    books = [
        {"symbol": l["id"], "contract": p["technical"]["contract"]}
        for p in packet["products"].values()
        for l in p["options"]
    ]
    receipts, blobs = collect_expiries(books, calendar=calendar)
    receipts = [
        r
        for r in receipts
        if datetime.fromisoformat(r["retrieved_at"]) <= min(_now(), cutoff)
    ]
    valid_expiry = {r["sha256"] for r in receipts}
    for checksum in valid_expiry:
        _store_blob(_folder(root), checksum, blobs[checksum])
    expiry = {b["symbol"]: b for b in books if b.get("expiry_sha256") in valid_expiry}
    for p in packet["products"].values():
        p["options"] = [
            leg
            | {
                "expiry_at": expiry[leg["id"]]["expiry_at"],
                "expiry_sha256": expiry[leg["id"]]["expiry_sha256"],
            }
            for leg in p["options"]
            if leg["id"] in expiry
        ]
    packet.update(
        market_observations=[
            {
                k: r[k]
                for k in (
                    "day",
                    "exchange",
                    "type",
                    "sha256",
                    "retrieved_at",
                    "disclosed_available_at",
                )
                if k in r
            }
            for (d, _, _), r in index.items()
            if d == prior[-1]
        ],
        monthly_source_status=monthly_status,
        rolling_mode="forward_paper",
        news_review_guidance=NEWS_REVIEW_GUIDANCE,
        source_gaps=news["gaps"],
        source_status=news["source_status"],
        authority_policy=news["authority_policy"],
        source_vintage=news["vintage_note"],
        positioning_policy=news["positioning_policy"],
        next_sessions=list(calendar[j : j + 6]),
        expiry_sources=receipts,
        paper_accounts={g: last["groups"][g]["account"] for g in GROUPS},
        previous_account_sha256=last["sha256"],
        evidence_frozen_at=_now().isoformat(),
        decision_instructions="按现场封存的三个延续研究组合分别判断。只使用所给收盘材料，未核验到期的腿不在可选范围。到期须覆盖计划持有期和下个可用开盘退出。既有仓位、前日净值和现金参与决策。每日允许全部观望。仅纸面开盘价代理。",
    )
    return packet


def _prefetch(root, last, day, calendar, *, stage_root=None):
    """盘后定时预采；仅采用截止前已落盘的完整版本。"""
    folder = (stage_root or _folder(root)) / "staged" / day
    now = _now()
    cutoff = _time(last["day"], 20)
    if not _time(last["day"], 16) <= now < cutoff:
        raise DataGap("资料预采窗口已关闭")
    versions = sorted(folder.glob("*.json"))
    if versions:
        previous = _read(versions[-1])
        stamp = datetime.fromisoformat(previous["recorded_at"])
        same_account = not stage_root or previous["packet"].get("previous_account_sha256") == last["sha256"]
        interval = 10 if now.hour == 19 else 45
        if same_account and (now - stamp).total_seconds() < interval * 60:
            return {"status": "evidence_ready", "staged_at": stamp.isoformat()}
    packet = _packet(root, last, day, calendar)
    if stage_root:
        packet["fixed_paper_accounts"] = packet.pop("paper_accounts")
    stamp = _now()
    if stamp >= cutoff:
        raise DataGap("本轮预采跨过20点，保留更早已封存的资料")
    _seal(
        folder / (stamp.strftime("%H%M%S%f") + ".json"),
        {"packet": packet, "recorded_at": stamp.isoformat()},
        before=cutoff,
    )
    return {"status": "evidence_ready", "staged_at": stamp.isoformat()}


def _prepare(root, last, day, calendar, model, cli):
    folder = _folder(root)
    deadline = _time(last["day"], 21)

    def check_clock():
        if not _time(last["day"], 20) <= _now() < deadline:
            raise DataGap("未能在最早夜盘开盘前完成并持久化真实决策")

    check_clock()
    path = folder / "inputs" / f"{day}.json"
    versions = sorted((folder / "staged" / day).glob("*.json"))
    if not versions:
        raise DataGap("20点以前没有实际封存的研究材料，本交易日不能生成盘前判断")
    stage = _read(versions[-1])
    if (
        not _time(last["day"], 16)
        <= datetime.fromisoformat(stage["recorded_at"])
        < _time(last["day"], 20)
    ):
        raise ValueError("预采资料没有在允许窗口内封存")
    packet = stage["packet"]
    _seal(path, {"packet": packet, "stage_sha256": stage["sha256"]})
    if (
        packet["previous_account_sha256"] != last["sha256"]
        or packet["trade_date"] != day
    ):
        raise ValueError("冻结输入的交易日或账户不匹配")
    seconds = (deadline - _now()).total_seconds()
    if seconds < 10:
        raise DataGap("盘前剩余时间不足以调用真实模型")
    try:
        decisions, runtime = decide(
            packet, model, cli, timeout=min(300, int(seconds) - 2)
        )
        check_clock()
        validate_decision(decisions, packet)
    except ValueError as exc:
        raise DataGap(f"模型回执未通过验收：{exc}") from exc
    if (
        not runtime.get("run_id")
        or runtime["model"] != model
        or not runtime.get("usage", {}).get("input_tokens")
        or not runtime.get("prompt_sha256")
    ):
        raise DataGap("缺少真实盘前模型回执")
    for choice in decisions.values():
        if choice["action"] == "enter":
            sessions = packet["next_sessions"]
            hold = choice["holding_days"]
            if len(sessions) <= hold:
                raise DataGap("缺少持有期和退出所需交易日历")
            expiry = {
                l["id"]: datetime.fromisoformat(l["expiry_at"])
                for l in packet["products"][choice["product"]]["options"]
            }
            if any(
                expiry[l] <= _time(sessions[hold], 9) + timedelta(minutes=5)
                for l in choice["legs"]
            ):
                raise DataGap("选择的期权在计划退出以前到期")
    stamp = _now()
    if stamp >= deadline:
        raise DataGap("模型回调超出盘前窗口")
    return _seal(
        folder / "decisions" / f"{day}.json",
        {
            "version": 1,
            "day": day,
            "prev_sha256": last["sha256"],
            "recorded_at": stamp.isoformat(),
            "packet": packet,
            "context_sha256": digest(packet),
            "decisions": decisions,
            "runtime": runtime,
        },
        before=deadline,
    )


def tick(
    workspace_root: Path, *, now: datetime | None = None, model="gpt-6.2-sol", cli=None
) -> dict:
    """每次最多日结一个交易日，收市前只允许准备尚未开盘的下一交易日。"""
    root = Path(workspace_root)
    folder = _folder(root)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        last = latest_account_snapshot(root) or _seed(root)
        last = _upgrade_seed(root, last)
        current = (now or _now()).astimezone(BJT)
        calendar = trading_days(root)
        idx = calendar.index(last["day"]) + 1
        status = {"last_settled": last["day"], "checked_at": current.isoformat()}
        if idx >= len(calendar):
            result = status | {"status": "calendar_exhausted"}
        else:
            day = calendar[idx]
            status["next_session"] = day
            try:
                if current >= _time(day, 16) and _now() >= _time(day, 16):
                    mark = _settle(root, last, day)
                    result = status | {
                        "status": "settled",
                        "last_settled": day,
                        "decision_status": mark["decision_status"],
                    }
                elif _time(last["day"], 16) <= current < _time(last["day"], 20):
                    result = status | _prefetch(root, last, day, calendar)
                elif (folder / "decisions" / f"{day}.json").exists():
                    receipt = _read(folder / "decisions" / f"{day}.json")
                    result = status | {
                        "status": "awaiting_close",
                        "decision_sha256": receipt["sha256"],
                    }
                elif _time(last["day"], 20) <= current < _time(last["day"], 21):
                    receipt = _prepare(
                        root, last, day, calendar, model, cli or _default_codex_bin()
                    )
                    result = status | {
                        "status": "sealed_preopen",
                        "decision_sha256": receipt["sha256"],
                    }
                else:
                    result = status | {
                        "status": "waiting",
                        "decision_window_start": _time(last["day"], 20).isoformat(),
                        "decision_deadline": _time(last["day"], 21).isoformat(),
                        "next_close_check": _time(day, 16).isoformat(),
                    }
            except (DataGap, TimeoutError, RuntimeError) as exc:
                result = status | {"status": "pending", "reason": str(exc)[:320]}
        atomic(folder / "status.json", result)
        return result
