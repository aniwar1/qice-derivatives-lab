"""为现场纸面研究绑定独立账本、可用现金与持仓风险。"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from pathlib import Path

from pydantic import Field, FiniteFloat, ValidationError, model_validator

from .models import PRODUCT_EXCHANGES, StrictModel


class PaperPosition(StrictModel):
    product: str
    contract: str = Field(pattern=r"^[A-Z]{1,3}\d{4}$")
    quantity: int = Field(gt=0)
    open_cost: FiniteFloat = Field(gt=0)

    @model_validator(mode="after")
    def contract_matches(self):
        if self.product not in PRODUCT_EXCHANGES or self.contract[:-4] != self.product:
            raise ValueError("模拟仓位品种与月份不一致")
        return self


class PaperSleeve(StrictModel):
    cash: FiniteFloat = Field(ge=0)
    equity: FiniteFloat = Field(ge=0)
    positions: list[PaperPosition] = Field(max_length=100)

    @model_validator(mode="after")
    def equity_covers_cash(self):
        if self.cash > self.equity or (not self.positions and abs(self.cash - self.equity) > 0.01):
            raise ValueError("期权买方模拟账户净值与现金不一致")
        return self


class ForwardPaperAccount(StrictModel):
    schema_version: str = Field(pattern=r"^forward_paper_account\.v1$")
    as_of: datetime
    valid_until: datetime
    sleeves: dict[str, PaperSleeve]


def paper_account(root: Path, cutoff: datetime) -> dict:
    """只接受决策截止前已存在且仍在有效期内的本地纸面快照。"""
    file = root / "local-data/forward-paper-account.json"
    missing = {
        "status": "unavailable",
        "source": "local-data/forward-paper-account.json",
        "as_of": None,
        "sleeves": None,
        "reason": "无有效现场模拟账本，现金、占仓与风险余额未知，仅可按参考本金提出独立条件计划。",
    }
    if not file.is_file():
        return missing
    try:
        stat = file.stat()
        if stat.st_size > 250_000 or stat.st_mtime > cutoff.timestamp():
            return missing
        raw = file.read_bytes()
        account = ForwardPaperAccount.model_validate_json(raw)
        if (
            account.as_of.utcoffset() is None
            or account.valid_until.utcoffset() is None
            or not account.as_of <= cutoff <= account.valid_until
            or set(account.sleeves) != set("ABC")
        ):
            return missing
    except (OSError, ValidationError, ValueError):
        return missing
    return {
        **account.model_dump(mode="json"),
        "status": "observed",
        "source": missing["source"],
        "sha256": sha256(raw).hexdigest(),
    }


def entry_budget(context: dict, group: str, product: str, timely_evidence: bool = True) -> float:
    """求当前组可用的现金、单笔上限和剩余总敞口三者的最小值。"""
    fraction = 0.005 if group == "A" or (group == "B" and not timely_evidence) else 0.01
    account = context.get("paper_account")
    if not account:
        # 旧版归档沿用原决策时冻结的预算。
        return context["entry_budget"] * (0.5 if group == "B" and not timely_evidence else 1)
    if account["status"] != "observed":
        return context["paper_equity_reference"] * fraction
    sleeve = account["sleeves"][group]
    positions = sleeve["positions"]
    if any(p["product"] == product for p in positions):
        return 0
    remaining = max(0, sleeve["equity"] * 0.08 - sum(p["open_cost"] for p in positions))
    return min(sleeve["cash"], sleeve["equity"] * fraction, remaining)


def directional_precheck(context: dict) -> dict:
    """列出能买整手的已观测盘口，方向与入选决定仍由 Agent 判断。"""
    cutoff = datetime.fromisoformat(context["cutoff"])
    focus = {r["contract"] for r in context["technical"]}
    results = {"A": [], "B": []}
    for group in results:
        if context["research_policy"]["per_group"][group] == "baseline":
            continue
        for product, multiplier in context["multipliers"].items():
            facts = [f for f in context["sources"]["items"] if product in f.get("products", [])]
            fundamental = any(
                f["dimension"] == "fundamentals"
                and f.get("freshness_role") != "low_frequency_background_only"
                and (group == "B" or f.get("scope") == "direct")
                for f in facts
            )
            news = any(f["dimension"] == "news" for f in facts)
            if group == "A" and not (fundamental and news):
                continue
            budget = entry_budget(context, group, product, fundamental and news)
            affordable = []
            for row in context["options"]["books"]:
                if row["contract"] not in focus or row["contract"][:-4] != product:
                    continue
                age = (cutoff - datetime.fromisoformat(row["observed_at"])).total_seconds()
                if not 0 <= age <= 120 or datetime.fromisoformat(row["available_at"]) > cutoff:
                    continue
                unit_risk = row["ask"] * multiplier + 2 * context["fee_per_leg"]
                lots = min(int(budget // unit_risk), row["bid_size"], row["ask_size"])
                if lots:
                    affordable.append({"symbol": row["symbol"], "max_lots": lots})
            if affordable:
                results[group].append({"product": product, "budget_cny": budget, "options": affordable})
    return results
