"""固定择强、预算与退出规则，比较每天允许入场和每五个交易日允许入场。"""

import argparse
from datetime import date, datetime, time
import gzip
from hashlib import sha256
import json
from pathlib import Path

from app.agent_replay import BJT, digest
from app.agent_replay_account import Account, INITIAL
from scripts.agent_replay import atomic
from scripts.frequency_replay import asof_nav
from scripts.expiry_settlement import ExpiryLedger

RULES = [
    "每组初始现金20万元。冻结前日实际合约的五日涨跌和年化波动率，按绝对涨跌/波动率排序；无二十日波动样本则不入围。",
    "顺趋势买入标的最近一档可负担的虚值期权。排序只读当时的报价；前日量至少100手，前收加1%及3元不超过净值0.5%。排除已有仓位的品种，每个允许日最多提一单。",
    "开盘按同一预算、前日量1%、总成本8%和原账户规则核验，不按开盘表现改选。标的反向3%或持满3个交易日触发次开退出。",
    "到期后，逐腿核交易所原件披露的到期日、期货结算价、期权结算价、未平仓与行权量；对确证虚值且未行权的组合归零解锁，实值转仓或依据缺失另行核对。",
    "每日允许与每五日允许仅改变入场日历。五种相位全部列出，后42日按已有账户的净值变化计算，包含从前段带入的持仓。各腿每边1%不利滑点及3元费用。",
]


def propose(packet, account):
    held = {position["product"] for position in account.positions}
    choices = []
    for code, item in packet["products"].items():
        t = item["technical"]
        if (
            code in held
            or not t
            or not t["change5_pct"]
            or not t["realized_vol20_ann_pct"]
        ):
            continue
        side = "C" if t["change5_pct"] > 0 else "P"
        for leg in sorted(
            item["options"],
            key=lambda leg: (abs(leg["strike"] - t["close"]), leg["id"]),
        ):
            if (
                leg["side"] == side
                and leg["volume"] >= 100
                and leg["premium"] * item["multiplier"] * 1.01 + 3
                <= min(account.cash, account.previous_equity * 0.005)
            ):
                score = abs(t["change5_pct"]) / t["realized_vol20_ann_pct"]
                choices.append(
                    (
                        -score,
                        code,
                        leg["id"],
                        t["close"] * (0.97 if side == "C" else 1.03),
                    )
                )
                break
    if not choices:
        return {"action": "wait", "thesis": "无满足预算与流动性的候选"}
    _, code, leg, stop = min(choices)
    return {
        "action": "enter",
        "product": code,
        "legs": [leg],
        "risk_bps": 50,
        "holding_days": 3,
        "invalidation_price": stop,
        "evidence_ids": [],
        "take_profit_close": 0,
        "max_entry_premium": 0,
        "max_lots": 0,
    }


def run(study, out):
    read = lambda name: json.loads((study / name).read_bytes())
    manifest = read("run-manifest.json")
    sources = read("market/source-manifest.json")
    packed = (study / "market/market.json.gz").read_bytes()
    if (
        sha256(packed).hexdigest() != manifest["market_sha256"]
        or digest(sources) != manifest["market_manifest_sha256"]
    ):
        raise ValueError("行情与冻结摘要不一致")
    market = json.loads(gzip.decompress(packed))
    expiry_ledger = ExpiryLedger(study, out, sources)
    late = {}
    for row in sources["sources"]:
        if row["type"] == "future" and row.get("disclosed_available_at"):
            cutoff = datetime.combine(date.fromisoformat(row["day"]), time(20), BJT)
            if datetime.fromisoformat(row["disclosed_available_at"]) > cutoff:
                late.setdefault(row["day"], set()).add(row["exchange"])
    metadata = {(r["day"], r["exchange"], r["type"]): r for r in sources["sources"]}
    original = read("results.json")["plans"][0]["sessions"]
    rules = {
        "rules": RULES,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_run_sha256": digest(manifest),
        "expiry_index_sha256": sha256((out / "expiry-index.json").read_bytes()).hexdigest(),
        "expiry_ledger_sha256": sha256(Path(__file__).with_name("expiry_settlement.py").read_bytes()).hexdigest(),
        "tail_days": 42,
    }
    atomic(out / "control-policy.json", rules)
    schedules = [("daily", 1, 0)] + [(f"fifth_{i}", 5, i) for i in range(5)]
    accounts = {key: Account("B") for key, _, _ in schedules}
    paths = {key: [] for key in accounts}
    for i, day in enumerate(manifest["calendar"]):
        packet = read(f"receipts/{manifest['contexts'][day]}/context.json")
        if original[i]["day"] != day or digest(packet) != original[i]["context_sha256"]:
            raise ValueError("决策时点资料摘要变化")
        exchanges = {
            code: item["exchange"] for code, item in packet["products"].items()
        }
        cutoff = datetime.fromisoformat(packet["cutoff"])
        for key, gap, phase in schedules:
            a = accounts[key]
            a.previous_equity = asof_nav(
                a, market, manifest["calendar"][:i], metadata, cutoff, exchanges
            )
            decision = (
                propose(packet, a)
                if i % gap == phase
                else {"action": "wait", "thesis": "本相位非入场日"}
            )
            order, point = expiry_ledger.process(
                a, day, decision, packet, market[day], late.get(day, ())
            )
            paths[key].append(
                {
                    "day": day,
                    "equity": point["equity"],
                    "stale_marks": point["stale_marks"],
                    "pending_exits": point["pending_exits"],
                    "decision": decision,
                    "execution": order,
                    "open_cost": round(sum(p["cost"] for p in a.positions), 2),
                }
            )
    summaries = []
    for key, _, _ in schedules:
        path, a = paths[key], accounts[key]
        summaries.append(
            {
                "id": key,
                "opens": a.opened,
                "pnl": round(path[-1]["equity"] - INITIAL, 2),
                "drawdown_pct": round(a.drawdown * 100, 5),
                "tail_pnl": round(path[-1]["equity"] - path[-43]["equity"], 2),
                "mean_cost_pct": round(
                    sum(p["open_cost"] / p["equity"] for p in path) * 100 / len(path), 4
                ),
            }
        )
    result = {
        "rules": RULES,
        "policy_sha256": digest(rules),
        "tail_start": manifest["calendar"][-42],
        "summary": summaries,
        "paths": paths,
    }
    atomic(out / "control.json", result)
    print(json.dumps(summaries, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.study, args.out)
