"""按已公布的逐腿截止日、期货结算价和终局期权表确认无行权价值的到期仓位。"""
from datetime import date, datetime, time
from functools import lru_cache
from hashlib import sha256
import gzip
import json
from pathlib import Path

from app.agent_replay import BJT, LEG
from app.agent_replay_account import INITIAL
from app.option_expiry import ExpiryIndex, historical_expiry


ABANDON_URL = "https://www.czce.com.cn/cn/content_file/flfg/zcjywgz/ywbf/2026/5/889576021a584f73abd8fdac4205188b.pdf"
ABANDON_HASH = "a4ec322042f6ead2f4fea711c52ae4af2d4447ce52132638e0417ab24572a51b"


class ExpiryLedger:
    def __init__(self, study, output, market_manifest):
        if sha256((output / "expiry-rules" / (ABANDON_HASH + ".pdf")).read_bytes()).hexdigest() != ABANDON_HASH:
            raise ValueError("到期行权规则原件摘要不一致")
        self.study = study
        self.index = ExpiryIndex(output / "expiry-index.json")
        self.calendar = market_manifest["calendar"]
        self.universe = market_manifest["configured_universe"]
        self.sources = {(s["day"], s["exchange"], s["type"]): s for s in market_manifest["sources"]}
        self.headers = {s["observed_sha256"]: s["last_modified_cst"] for s in
                        json.loads((study / "market/vintage-verification.json").read_bytes())["files"]
                        if not s["issues"] and s["observed_sha256"] == s["archived_sha256"] == s["expected_sha256"]}

    @lru_cache(maxsize=24)
    def _czce_table(self, day, kind):
        s = self.sources[day, "CZCE", kind]
        raw = gzip.decompress((self.study / "market" / s["raw"]).read_bytes())
        if sha256(raw).hexdigest() != s["sha256"]:
            raise ValueError("到期期货或期权原件摘要不一致")
        title, header, *lines = raw.decode("utf-8-sig").splitlines()
        if day not in title:
            raise ValueError("终局文件日期错误")
        columns = [x.strip() for x in header.split("|")]
        if columns[0] != "合约代码":
            raise ValueError("到期合约字段变化")
        rows = {}
        for line in lines:
            cells = [x.strip().replace(",", "") for x in line.split("|")]
            rows[cells[0]] = dict(zip(columns, cells))
        return rows, s, datetime.fromisoformat(self.headers[s["sha256"]])

    def outcome(self, pos, day):
        cutoff = datetime.combine(date.fromisoformat(day), time(20), BJT)
        observed = [self.index.lookup(leg, cutoff) for leg in pos["legs"]]
        if not all(observed):
            estimate = historical_expiry(pos["underlying"], self.calendar)
            if estimate and estimate <= cutoff.date():
                raise ValueError(f"持仓已到规则期限但缺少逐腿公告：{pos['legs']}")
            return None
        dates = {row["expiry"] for row in observed}
        if len(dates) != 1:
            raise ValueError("到期日不一致的多腿不能一起结算")
        exp = dates.pop()
        if exp > day:
            return None
        if self.universe[pos["product"]] != "CZCE":
            raise ValueError(f"需要对应交易所的行权或放弃核对：{pos['legs']}")
        futures, fs, fut_time = self._czce_table(exp, "future")
        options, os, opt_time = self._czce_table(exp, "option")
        if max(fut_time, opt_time) > cutoff:
            return None
        p, month = pos["product"], pos["underlying"][-3:]
        settlement = float(futures[p + month]["今结算"])
        facts = []
        for symbol in pos["legs"]:
            m = LEG.fullmatch(symbol)
            strike, side = float(m[4]), m[3]
            row = options[p + month + side + m[4]]
            intrinsic = (settlement - strike) * (1 if side == "C" else -1)
            if intrinsic > 0 or any(float(row[k]) != 0 for k in ("今结算", "持仓量", "行权量")):
                raise ValueError(f"待确认行权、期货转仓或结算，不得推定现金结清：{symbol}")
            facts.append({"leg":symbol,"strike":strike,"option_settlement":0,"final_oi":0,"exercise_volume":0})
        return {"expiry":exp, "settled_on":day, "future_settlement":settlement, "options":facts,
                "reference_sha256":[r["sha256"] for r in observed], "future_sha256":fs["sha256"],
                "option_sha256":os["sha256"], "abandon_rule_sha256":ABANDON_HASH, "abandon_rule_url":ABANDON_URL,
                "available_at":max(fut_time,opt_time).isoformat()}

    def process(self, account, day, decision, packet, prices, delayed=()):
        prior_peak, prior_drawdown = account.peak, account.drawdown
        order, point = account.process(day, decision, packet, prices, delayed)
        for pos in list(account.positions):
            certificate = self.outcome(pos, day)
            if certificate is None:
                continue
            account.positions.remove(pos)
            account.closed += 1
            account.realized -= pos["cost"]
            order["exits"].append({"product":pos["product"], "entry_day":pos["day"], "legs":pos["legs"],
                                   "quantity":pos["qty"], "proceeds_after_costs":0.0, "opening_debit":round(pos["cost"],2),
                                   "pnl":round(-pos["cost"],2), "reason":"到期虚值且自动放弃，本组合回收为零",
                                   "expiry_evidence":certificate})
        if any("expiry_evidence" in row for row in order["exits"]):
            equity = account.cash + sum(p["value"] for p in account.positions)
            account.peak = max(prior_peak,equity)
            account.drawdown = max(prior_drawdown,1 - equity/account.peak)
            account.previous_equity = equity
            point.update(equity=round(equity,2), return_pct=round((equity/INITIAL-1)*100,5),
                         realized=round(account.realized,2), open_positions=len(account.positions),
                         open_pnl=round(sum(p["value"]-p["cost"] for p in account.positions),2),
                         pending_exits=sum(bool(p["exit_reason"]) for p in account.positions),
                         stale_marks=[p for p in point["stale_marks"] if any(i["product"]==p for i in account.positions)])
        return order, point
