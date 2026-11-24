"""从当日交易所原表核对薄成交估值及到期处理。"""

import gzip
import json
from datetime import date
from hashlib import sha256
from math import isfinite

import requests

from scripts.collect_replay_market import atomic
from scripts.expiry_settlement import ABANDON_HASH, ABANDON_URL

from .agent_replay import LEG, positive
from .agent_replay_account import INITIAL, closing_details, position_value
from .live_expiry import expiry_rows
from .models import PRODUCT_EXCHANGES
from .option_expiry import ExpiryIndex


def _market_code(leg, exchange):
    if exchange != "CZCE":
        return leg
    match = LEG.fullmatch(leg)
    if match:
        return match[1] + match[2][-3:] + match[3] + match[4]
    return leg[:-4] + leg[-3:]


def inherited_expiries(root, base, accounts, cutoff):
    """用旧研究逐腿披露原件核实期初持仓的到期时刻，并封存同一份凭证。"""
    positions = [
        p for a in accounts.values() for p in a.positions if "expiry_legs" not in p
    ]
    if not positions:
        return
    study = base.with_name(base.name + "-frequency")
    path = study / "expiry-index.json"
    index = ExpiryIndex(path)
    files = {s["sha256"]: s for s in json.loads(path.read_bytes())["sources"]}
    folder = root / "local-data/rolling-six/evidence"
    for pos in positions:
        refs = {}
        for leg in pos["legs"]:
            quote = index.lookup(leg, cutoff)
            if not quote:
                raise LookupError(f"缺少期初在手合约的交易所期限原表：{leg}")
            key = quote["sha256"]
            src = files[key]
            raw = gzip.decompress(
                (study / "expiry-originals" / src["raw"]).read_bytes()
            )
            if sha256(raw).hexdigest() != key:
                raise ValueError("期初到期原件摘要不一致")
            response = requests.Response()
            response._content = raw
            exchange = PRODUCT_EXCHANGES[pos["product"]]
            end = expiry_rows(exchange, date.fromisoformat(src["day"]), response).get(
                _market_code(leg, exchange)
            )
            if not end or end[:10] != quote["expiry"]:
                raise ValueError("逐腿到期日与索引不一致")
            dest = folder / key
            if dest.exists() and sha256(dest.read_bytes()).hexdigest() != key:
                raise ValueError("期初到期原件已改变")
            if not dest.exists():
                atomic(dest, raw)
            refs[leg] = {"expiry_at": end, "expiry_sha256": key}
        pos["expiry_legs"] = refs


def _daily_table(root, day, sources, exchange, kind, market_dir=None):
    source = next(
        (
            r
            for r in sources
            if r["exchange"] == exchange and r["type"] == kind and r["day"] == day
        ),
        None,
    )
    if not source:
        raise LookupError(f"{day} 缺少{exchange} {kind}结算原件")
    archive = market_dir or root / "local-data/rolling-six/market"
    blob = gzip.decompress((archive / source["raw"]).read_bytes())
    if sha256(blob).hexdigest() != source["sha256"]:
        raise ValueError("行情原件摘要不一致")
    rows = {}
    if exchange == "CZCE":
        title, header, *lines = blob.decode("utf-8-sig").splitlines()
        if day not in title or ("期货" if kind == "future" else "期权") not in title:
            raise ValueError("结算原件日期或类型不符")
        columns = [x.strip() for x in header.split("|")]
        for line in lines:
            row = dict(
                zip(columns, (x.strip().replace(",", "") for x in line.split("|")))
            )
            rows[row["合约代码"]] = (
                row.get("今结算"),
                row.get("持仓量"),
                row.get("行权量"),
            )
    elif exchange == "SHFE":
        body = json.loads(blob)
        if body["report_date"] != day.replace("-", ""):
            raise ValueError("上期所结算原件日期不符")
        for r in body["o_curinstrument"]:
            name = (
                r.get("INSTRUMENTID")
                if kind == "option"
                else str(r["PRODUCTGROUPID"]).strip() + str(r["DELIVERYMONTH"]).strip()
            )
            if name:
                rows[name.strip().upper()] = (
                    r.get("SETTLEMENTPRICE"),
                    r.get("OPENINTEREST"),
                    r.get("EXECVOLUME"),
                )
    else:
        raise LookupError(f"{exchange} 尚无已核验的结算字段，等待交易或原表确认")
    return rows, source


def _number(value, context):
    try:
        result = float(str(value).strip().replace(",", ""))
    except (ValueError, TypeError):
        raise LookupError(f"缺少{context}数值") from None
    if not isfinite(result) or result < 0:
        raise LookupError(f"{context}数值无效")
    return result


def _abandonment(root, pos, expiry, day, rows):
    """仅在郑商所证实到期无内在价值、期权结算和持仓行权量全零时结清。"""
    exchange = PRODUCT_EXCHANGES[pos["product"]]
    if expiry != day or exchange != "CZCE":
        raise LookupError(f"{pos['legs']} 到期，需核对交易所行权或放弃结果")
    futures, fs = rows(exchange, "future")
    options, os = rows(exchange, "option")
    name = _market_code(pos["underlying"], exchange)
    delivery = _number(futures.get(name, (None,))[0], "期货今结算")
    if not positive(delivery):
        raise LookupError("缺少标的最后结算价")
    for leg in pos["legs"]:
        m = LEG.fullmatch(leg)
        strike = float(m[4])
        row = options.get(_market_code(leg, exchange))
        if (
            not row
            or (delivery - strike) * (1 if m[3] == "C" else -1) > 0
            or any(_number(x, "期权终局") != 0 for x in row)
        ):
            raise LookupError(f"{leg} 到期，待核对行权或期货转仓，不能作零值处理")
    from .study_view import BATCH

    rule = (
        root
        / "local-data/agent-study"
        / (BATCH + "-frequency")
        / "expiry-rules"
        / (ABANDON_HASH + ".pdf")
    )
    if not rule.is_file() or sha256(rule.read_bytes()).hexdigest() != ABANDON_HASH:
        raise LookupError("缺少已核验的自动放弃业务规则")
    return {
        "expiry": day,
        "future_settlement": delivery,
        "rule_sha256": ABANDON_HASH,
        "rule_url": ABANDON_URL,
        "future_sha256": fs["sha256"],
        "option_sha256": os["sha256"],
        "expiry_legs": pos["expiry_legs"],
    }


def finish_marks(
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
    *,
    market_dir=None,
):
    """先执行原成交规则，再按当日真实价格校正未成交腿的账面估值。"""
    tables = {}

    def rows(exchange, kind):
        key = (exchange, kind)
        if key not in tables:
            tables[key] = _daily_table(root, day, sources, exchange, kind, market_dir)
        return tables[key]

    used = []
    for pos in list(account.positions):
        if "expiry_legs" not in pos and pos["day"] == day:
            quotes = {l["id"]: l for l in packet["products"][pos["product"]]["options"]}
            pos["expiry_legs"] = {
                l: {k: quotes[l][k] for k in ("expiry_at", "expiry_sha256")}
                for l in pos["legs"]
            }
        expiry = {
            (pos.get("expiry_legs") or {}).get(l, {}).get("expiry_at", "")[:10]
            for l in pos["legs"]
        }
        if len(expiry) != 1 or not all(expiry):
            raise LookupError(f"{pos['legs']} 缺少一致的逐腿期限")
        end = expiry.pop()
        exchange = PRODUCT_EXCHANGES[pos["product"]]
        if end <= day:
            certificate = _abandonment(root, pos, end, day, rows)
            account.positions.remove(pos)
            account.closed += 1
            account.realized -= pos["cost"]
            order["exits"].append(
                {
                    "product": pos["product"],
                    "entry_day": pos["day"],
                    "legs": pos["legs"],
                    **closing_details(pos, [0.0] * len(pos["legs"])),
                    "pnl": round(-pos["cost"], 2),
                    "opening_debit": round(pos["cost"], 2),
                    "proceeds_after_costs": 0.0,
                    "reason": "到期虚值且交易所终局证据确认放弃",
                    "expiry_evidence": certificate,
                }
            )
            continue
        future = prices["future"].get(pos["underlying"])
        if not future or not positive(future[3]):
            raise LookupError(
                f"{day} 缺少实际在手月份的标的收盘价：{pos['underlying']}"
            )
        if pos["product"] not in point["stale_marks"]:
            continue
        values = []
        for leg in pos["legs"]:
            row = prices["option"].get(leg)
            if row and positive(row[3]) and positive(row[4]):
                values.append(row[3])
                continue
            table, rec = rows(exchange, "option")
            settled = _number(
                table.get(_market_code(leg, exchange), (None,))[0], "当日期权结算"
            )
            used.append(
                {"leg": leg, "price": settled, "day": day, "sha256": rec["sha256"]}
            )
            values.append(settled)
        pos["value"] = position_value(pos, values)

    equity = account.cash + sum(p["value"] for p in account.positions)
    account.previous_equity = equity
    account.peak = max(prior_peak, equity)
    account.drawdown = max(prior_drawdown, 1 - equity / account.peak)
    point.update(
        equity=round(equity, 2),
        return_pct=round(100 * (equity / INITIAL - 1), 5),
        realized=round(account.realized, 2),
        open_pnl=round(sum(p["value"] - p["cost"] for p in account.positions), 2),
        open_positions=len(account.positions),
        pending_exits=sum(bool(p["exit_reason"]) for p in account.positions),
        stale_marks=[],
        official_settlement_marks=used,
    )
