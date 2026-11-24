"""日更只为定额版形成新判断，已落盘的旧交易所原表继续用于取证。"""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import fcntl

from .agent_replay import BJT, digest
from .agent_replay_account import FIXED_STRATEGY_VERSION, SETTLEMENT_REVISION
from .config import _default_codex_bin
from .july_policy import record
from .models import PRODUCT_EXCHANGES
from .rolling_calendar import trading_days
from .rolling_update import DataGap, _market, _prefetch, _read, _seal, _seed, _time, latest_account_snapshot
from scripts.agent_replay import atomic, load
from scripts.july_settlement import build, refresh, selected_policy


def current_market(root, folder):
    paths = sorted((folder / "market-days").glob("*.json"))
    if paths:
        return _read(paths[-1])
    return latest_account_snapshot(root) or _seed(root)


def _current_ledger(root, ledger, day):
    return bool(
        ledger and ledger.get("end") == day
        and ledger.get("version") == FIXED_STRATEGY_VERSION
        and ledger.get("settlement_revision") == SETTLEMENT_REVISION
        and ledger.get("execution_policy") == selected_policy(root)
    )


def capture_day(root, folder, last, day):
    sealed = folder / "decisions-v2" / f"{day}.json"
    # 开盘前收到的计划才可进入前向样本。
    submitted = _read(sealed) if sealed.exists() else None
    if submitted and (submitted["day"] != day or submitted["mode"] != "preopen"):
        raise ValueError("日结引用的盘前回执身份无效")
    ledger_path = folder / "settlement-v2.json"
    if not ledger_path.exists():
        raise DataGap("期初账户尚未核对")
    ledger = load(ledger_path)
    if not _current_ledger(root, ledger, last["day"]):
        ledger = refresh(root)
    if not _current_ledger(root, ledger, last["day"]):
        raise DataGap("待接续账户与当前执行规则或最近日表日期不一致")
    required = {pos["product"] for group in ledger["groups"].values() for pos in group["account"]["positions"]}
    if submitted:
        required.update(d["product"] for d in submitted["decisions"].values() if d["action"] == "enter")
    exchanges = {PRODUCT_EXCHANGES[p] for p in required}
    prices, sources, gaps = _market(root, day, refresh_exchanges=exchanges)
    if not prices["future"] or not prices["option"]:
        raise DataGap("当日期货或期权日表尚未取得")
    seen = {(s["exchange"], s["type"]) for s in sources}
    for product in sorted(required):
        if any((PRODUCT_EXCHANGES[product], kind) not in seen for kind in ("future", "option")):
            raise DataGap(f"{product} 的持仓或委托行情不完整，待下一轮采集")
    if submitted:
        for order in submitted["decisions"].values():
            if order["action"] != "enter":
                continue
            underlying = submitted["packet"]["products"][order["product"]]["technical"]["contract"]
            if underlying not in prices["future"] or any(leg not in prices["option"] for leg in order["legs"]):
                raise DataGap(f"{day} {order['product']} 缺少委托对应合约的日表记录")
    candidate = {
        "day": day, "baseline_end": last["baseline_end"], "prev_sha256": last["sha256"],
        "market_sources": sources, "market_gaps": gaps, "recorded_at": datetime.now(BJT).isoformat(),
        "decision_status": "sealed_preopen" if submitted else "missed_preopen",
        "decision_sha256": submitted["decision_sha256"] if submitted else None,
    }
    # 用正式结算读取原件和在手合约；交易日推进之前须能完成三组账目。
    with TemporaryDirectory(dir=folder) as staging:
        path = Path(staging) / f"{day}.json"
        _seal(path, candidate)
        report = build(root, candidate=(day, path))
        if report["end"] != day:
            raise DataGap("；".join(report["issues"]) or f"{day} 候选行情尚不能结算")
        return _seal(folder / "market-days" / f"{day}.json", candidate)


def _prepare(root, folder, last, day, model, cli, now):
    stages = sorted((folder / "staged" / day).glob("*.json"))
    if not stages:
        raise DataGap("没有在当日20点前封存的定额策略材料")
    stage = _read(stages[-1])
    packet = stage["packet"]
    stamp = datetime.fromisoformat(stage["recorded_at"])
    if (not _time(last["day"], 16) <= stamp < _time(last["day"], 20)
            or packet["trade_date"] != day or packet["previous_account_sha256"] != last["sha256"]):
        raise ValueError("盘前输入日期、可得时点或账户不匹配")
    deadline = _time(last["day"], 21)
    if not _time(last["day"], 20) <= now < deadline:
        raise DataGap("超出盘前窗口")
    return record(root, packet, model, cli, mode="preopen", deadline=deadline)


def tick(root: Path, *, model="gpt-6.2-sol", cli=None, now=None):
    folder = root / "local-data/july-strategy"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "daily.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        current = now or datetime.now(BJT)
        status = {"checked_at": current.isoformat(), "status": "waiting"}
        try:
            last = current_market(root, folder)
            calendar = trading_days(root)
            i = calendar.index(last["day"]) + 1
            if i == len(calendar):
                return status | {"status": "calendar_exhausted"}
            day = calendar[i]
            status.update(last_settled=last["day"], next_session=day)
            saved = folder / "settlement-v2.json"
            if current >= _time(day, 16) and (not saved.exists() or not _current_ledger(root, load(saved), last["day"])):
                refresh(root)
            if current >= _time(day, 16):
                last = capture_day(root, folder, last, day)
                status.update(last_settled=day, status="settled")
            dependencies = [p.stat().st_mtime_ns for group in ("decisions-v2", "market-days")
                            for p in (folder / group).glob("*.json")]
            ledger = load(saved) if saved.exists() else None
            if not _current_ledger(root, ledger, last["day"]) or max(dependencies, default=0) > saved.stat().st_mtime_ns:
                ledger = refresh(root)
            if not _current_ledger(root, ledger, last["day"]):
                raise DataGap("当前执行规则账目尚未结算至最近交易日")
            if day != last["day"]:
                view = last | {"groups": ledger["groups"]}
                # 冻结实际使用的定额账户和市场原表共同的版本。
                view["sha256"] = digest([last["sha256"], {g: row["account"] for g, row in ledger["groups"].items()}])
                if _time(last["day"], 16) <= current < _time(last["day"], 20):
                    status.update(_prefetch(root, view, day, calendar, stage_root=folder))
                elif (folder / "decisions-v2" / (day + ".json")).exists():
                    status["status"] = "awaiting_close"
                elif _time(last["day"], 20) <= current < _time(last["day"], 21):
                    _prepare(root, folder, view, day, model, cli or _default_codex_bin(), current)
                    status["status"] = "sealed_preopen"
        except (DataGap, OSError, ValueError, RuntimeError, TimeoutError) as exc:
            status.update(status="pending", reason=str(exc))
        atomic(folder / "daily-status.json", status)
        return status
