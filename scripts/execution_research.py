"""在训练期选择执行规则，冻结选择后仅验证一次。"""

import argparse
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from app import agent_replay_account, execution_research, second_entry
from app.agent_replay import BJT, GROUPS, digest
from app.agent_replay_account import INITIAL
from scripts.agent_replay import atomic, load
from scripts.july_settlement import build


def metrics(plan, start, end):
    balance = peak = INITIAL
    rows = [r for r in plan["points"] if r["day"] < start]
    if rows:
        balance = peak = rows[-1]["equity"]
    drawdown = 0.0
    daily = [row for row in plan["points"] if start <= row["day"] <= end]
    if not daily or daily[-1]["day"] != end:
        raise ValueError(f"{end} 日结尚未完成")
    for row in daily:
        peak = max(peak, row["equity"])
        drawdown = max(drawdown, (1 - row["equity"] / peak) * 100)
    executions = [s["execution"] for s in plan["sessions"] if start <= s["day"] <= end]
    return {"trading_days": len(daily), "opened": sum(e["status"] == "simulated_open" for e in executions),
            "closed": sum(len(e["exits"]) for e in executions), "net_pnl": round(daily[-1]["equity"] - balance, 2),
            "return_pct": round(100 * (daily[-1]["equity"] / balance - 1), 5), "max_drawdown_pct": round(drawdown, 5),
            "limit_proxy_fills": sum(e.get("fill_method") == "daily_limit_proxy" for e in executions)}


def qualifies(result, base):
    return (result["opened"] > base["opened"] and result["net_pnl"] > base["net_pnl"]
            and result["max_drawdown_pct"] <= base["max_drawdown_pct"] + 1)


def receipt_index(root, end):
    folder = root / "local-data/july-strategy/decisions-v2"
    return digest({p.stem: sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*.json")) if p.stem <= end})


def study(root, folder, protocol_path, validate=False):
    protocol = load(protocol_path)
    signature = sha256(protocol_path.read_bytes()).hexdigest()
    train = folder / "training.json"
    final = folder / "validation.json"
    start, split, end = [protocol[k] for k in ["start", "training_end", "validation_end"]]
    train_hash = receipt_index(root, split)
    mechanism = second_entry if "second_signal" in protocol["variants"] else execution_research
    implementation = sha256(Path(mechanism.__file__).read_bytes()).hexdigest()
    if protocol.get("prior_protocols"):
        for name, checksum in protocol["prior_protocols"].items():
            if sha256((protocol_path.parent / name).read_bytes()).hexdigest() != checksum:
                raise ValueError("先前登记的研究方案摘要不符")
        implementation = digest({
            module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (agent_replay_account, execution_research, second_entry)
        })
    if not train.exists():
        if validate:
            raise ValueError("须先保存训练期选择")
        table = {}
        for variant in protocol["variants"]:
            ledger = build(root, policy_by_group={g: variant for g in GROUPS}, stop_at=split)
            table[variant] = {g: metrics(ledger["groups"][g], start, split) for g in GROUPS}
        selected = {}
        for group in GROUPS:
            eligible = [k for k in protocol["variants"][1:] if qualifies(table[k][group], table["opening"][group])]
            selected[group] = max(eligible, key=lambda k: (table[k][group]["net_pnl"], -table[k][group]["max_drawdown_pct"])) if eligible else "opening"
        atomic(train, {"protocol_sha256": signature, "execution_sha256": implementation, "receipt_sha256": train_hash, "period": [start, split],
                       "recorded_at": datetime.now(BJT).isoformat(), "results": table, "selected": selected})
    trained = load(train)
    if (trained["protocol_sha256"] != signature or trained["receipt_sha256"] != train_hash
            or trained["execution_sha256"] != implementation):
        raise ValueError("训练期材料与冻结规则不一致")
    if not validate:
        return trained
    validation_hash = receipt_index(root, end)
    if final.exists():
        report = load(final)
        if report["receipt_sha256"] != validation_hash or report["training_sha256"] != digest(trained):
            raise ValueError("留出验证材料发生变动")
        return report
    baseline = build(root, policy_by_group={g: "opening" for g in GROUPS}, stop_at=end)
    selected = build(root, policy_by_group=trained["selected"], stop_at=end)
    result = {"training_sha256": digest(trained), "receipt_sha256": validation_hash,
              "recorded_at": datetime.now(BJT).isoformat(), "period": ["2026-10-01", end], "groups": {}, "active": {}}
    for g in GROUPS:
        old = metrics(baseline["groups"][g], "2026-10-01", end)
        new = metrics(selected["groups"][g], "2026-10-01", end)
        result["groups"][g] = {"candidate": trained["selected"][g], "opening": old, "selected": new}
        result["active"][g] = trained["selected"][g] if qualifies(new, old) else "opening"
    atomic(final, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--full-budget", action="store_true", help="以完整整手统一复核此前登记的所有规则")
    parser.add_argument("--addition", action="store_true", help="评估独立登记的第二笔委托方案")
    args = parser.parse_args()
    name = "scale-entry-study" if args.addition else "execution-study"
    if args.full_budget:
        name = "full-budget-execution-study"
    folder = args.workspace / "local-data/july-strategy" / name
    folder.mkdir(parents=True, exist_ok=True)
    protocol = Path(__file__).resolve().parents[1] / "config" / ("scale_entry_study.json" if args.addition else "execution_study.json")
    if args.full_budget:
        protocol = protocol.with_name("full_budget_execution_study.json")
    report = study(args.workspace, folder, protocol, args.validate)
    print(report.get("active", report.get("selected")))


if __name__ == "__main__":
    main()
