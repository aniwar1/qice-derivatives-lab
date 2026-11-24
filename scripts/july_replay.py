"""按历史可见的逐日材料独立重跑定额版三策略。"""

import argparse
import fcntl
import gzip
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from app.agent_replay import digest
from app.config import _default_codex_bin
from app.july_policy import START, record
from app.market_psychology import attach_reaction_history
from app.option_expiry import ExpiryIndex
from app.rolling_calendar import trading_days
from app.rolling_update import _read
from app.study_view import BATCH
from scripts.agent_replay import load, matches_archived_identity


def historical_packets(root):
    study = root / "local-data/agent-study" / BATCH
    manifest = load(study / "run-manifest.json")
    sources = load(study / "market/source-manifest.json")
    blob = (study / "market/market.json.gz").read_bytes()
    if sha256(blob).hexdigest() != manifest["market_sha256"] or digest(sources) != manifest["market_manifest_sha256"]:
        raise ValueError("历史行情或来源清单摘要不一致")
    market = json.loads(gzip.decompress(blob))
    # 复盘以原件披露时点为可见时点。缺少披露时点的日线沿原档案的日终假设。
    index = {}
    for row in sources["sources"]:
        available = row.get("disclosed_available_at") or row["day"] + "T20:00:00+08:00"
        index[row["day"], row["exchange"], row["type"]] = row | {"historical_available_at": available}
    expiries = study.with_name(BATCH + "-frequency")
    expiry_path = expiries / "expiry-index.json"
    checked = expiries / "ranking-verification.json"
    if checked.is_file() and sha256(expiry_path.read_bytes()).hexdigest() != load(checked)["expiry_index_sha256"]:
        raise ValueError("逐腿到期索引与原件核验记录不一致")
    expiry = ExpiryIndex(expiry_path)
    calendar = trading_days(root)
    for day in manifest["calendar"]:
        if day < START:
            continue
        key = manifest["contexts"][day]
        packet = load(study / "receipts" / key / "context.json")
        if packet["trade_date"] != day or not matches_archived_identity(packet, manifest["model"], key):
            raise ValueError("日期或历史资料摘要不符")
        cutoff = datetime.fromisoformat(packet["cutoff"])
        i = calendar.index(day)
        attach_reaction_history(packet, calendar[max(0, i - 5):i], market, index, historical=True)
        packet["next_sessions"] = calendar[i:i + 6]
        for item in packet["products"].values():
            options = []
            for leg in item["options"]:
                ref = expiry.lookup(leg["id"], cutoff)
                if ref and ref["expiry"] >= day:
                    options.append(leg | {"expiry_at": ref["expiry"] + "T15:00:00+08:00", "expiry_sha256": ref["sha256"]})
            item["options"] = options
        yield packet


def replay_day(root, source, model):
    parent_path = root / "local-data/july-strategy/decisions" / (source["trade_date"] + ".json")
    parent = load(parent_path) if parent_path.exists() else None
    if parent:
        # 逐字段核对复用的原始决策输入；仅预算合同改变。
        excluded = {"risk_context", "strategy_version", "news_review_guidance", "paper_accounts",
                    "previous_account_sha256", "decision_instructions", "rolling_mode"}
        left = {k: v for k, v in source.items() if k not in excluded}
        right = {k: v for k, v in parent["packet"].items() if k not in excluded}
        if digest(left) != digest(right):
            raise ValueError("分组复用的冻结资料不同")
    return record(root, source, model, _default_codex_bin(), mode="historical_replay", parent=parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default="gpt-6.2-sol")
    args = parser.parse_args()
    root = args.workspace
    folder = root / "local-data/july-strategy"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "replay.lock").open("a") as lock, ThreadPoolExecutor(args.workers) as pool:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows = []
        # 11月24日以前已结束的盘前观测均按新版历史再现标识。
        for path in sorted((root / "local-data/rolling-six/inputs").glob("*.json"), reverse=True):
            if START <= path.stem <= "2026-11-24":
                rows.append(_read(path)["packet"])
        tasks = {}
        for row in rows:
            task = pool.submit(replay_day, root, row, args.model)
            tasks[task] = row["trade_date"]
        for row in historical_packets(root):
            task = pool.submit(replay_day, root, row, args.model)
            tasks[task] = row["trade_date"]
        errors = []
        for task in as_completed(tasks):
            try:
                task.result()
                print(tasks[task], "完成", flush=True)
            except Exception as exc:
                errors.append(tasks[task])
                print(tasks[task], type(exc).__name__, str(exc)[:250], flush=True)
        if errors:
            raise SystemExit(f"待补回执：{len(errors)}")


if __name__ == "__main__":
    main()
