"""按期权腿读取交易所公布的最后交易日，保存对应原表。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests

from .models import PRODUCT_EXCHANGES

BJT = ZoneInfo("Asia/Shanghai")


def expiry_rows(exchange: str, day: date, response: requests.Response) -> dict[str, str]:
    """沪市按表内交易日匹配，郑市只接受最后交易日与到期时间相符的常规期权。"""
    if exchange == "SHFE":
        return {
            row["INSTRUMENTID"].strip().upper(): datetime.combine(
                date.fromisoformat(row["EXPIREDATE"]), time(15), BJT
            ).isoformat()
            for row in response.json()["OptionContractBaseInfo"]
            if row["TRADINGDAY"] == day.strftime("%Y%m%d")
        }
    result = {}
    for row in ElementTree.fromstring(response.content).iterfind("Contract"):
        if row.findtext("PrdTp") != "常规期权" or row.findtext("ExchCd") != "XZCE":
            continue
        expiry = row.findtext("ExpiryTime", "").removesuffix(" 北京时间")
        try:
            end = datetime.strptime(expiry, "%Y-%m-%d %H:%M").replace(tzinfo=BJT)
        except ValueError:
            continue
        if end.date().isoformat() == row.findtext("LstTrdDt") == row.findtext("ExpiryDt"):
            result[row.findtext("CtrCd", "").strip().upper()] = end.isoformat()
    return result


def collect_expiries(books: list[dict]) -> tuple[list[dict], dict[str, bytes]]:
    """按交易所批量下载合约参数，将每条有效期权腿绑定到原表摘要。"""
    if not books:
        return [], {}
    from akshare.futures.cons import get_calendar

    now = datetime.now(BJT)
    days = sorted(date.fromisoformat(v) for v in get_calendar() if date.fromisoformat(v) <= now.date())[-2:]
    receipts, originals = [], {}
    for exchange in {PRODUCT_EXCHANGES[b["contract"][:-4]] for b in books} & {"CZCE", "SHFE"}:
        for day in reversed(days):
            if (now.date() - day).days > 10:
                break
            url = (
                f"https://www.shfe.com.cn/data/busiparamdata/option/ContractBaseInfo{day:%Y%m%d}.dat"
                if exchange == "SHFE"
                else f"https://www.czce.com.cn/cn/DFSStaticFiles/Option/{day:%Y}/{day:%Y%m%d}/OptionDataReferenceData.xml"
            )
            try:
                response = requests.get(url, timeout=(2, 5), allow_redirects=False)
                if response.status_code != 200:
                    continue
                retrieved = datetime.now(BJT)
                modified = response.headers.get("Last-Modified")
                if modified and parsedate_to_datetime(modified) > retrieved:
                    continue
                lookup = expiry_rows(exchange, day, response)
            except (requests.RequestException, ElementTree.ParseError, KeyError, TypeError, ValueError):
                continue
            if not lookup:
                continue
            digest = sha256(response.content).hexdigest()
            for book in books:
                product = book["contract"][:-4]
                if PRODUCT_EXCHANGES[product] != exchange:
                    continue
                symbol = book["symbol"].upper().replace("-", "")
                if exchange == "CZCE" and symbol.startswith(book["contract"]):
                    symbol = product + symbol[len(product) + 1 :]
                if symbol in lookup:
                    book.update(expiry_at=lookup[symbol], expiry_sha256=digest)
            if any(b.get("expiry_sha256") == digest for b in books):
                originals[digest] = response.content
                receipts.append(
                    {
                        "exchange": exchange,
                        "reference_day": day.isoformat(),
                        "url": url,
                        "sha256": digest,
                        "retrieved_at": retrieved.isoformat(),
                        "last_modified": modified,
                    }
                )
            break
    return receipts, originals


def pair_survives(ticks: list[dict], context: dict, last: datetime) -> bool:
    """两腿均须有冻结的交易所凭证，且计划退出严格早于最后可交易时刻。"""
    cutoff = datetime.fromisoformat(context["cutoff"])
    refs = {
        r["sha256"]
        for r in context["options"].get("expiry_sources", [])
        if datetime.fromisoformat(r["retrieved_at"]) <= cutoff
    }
    return all(
        t.get("expiry_sha256") in refs
        and t.get("expiry_at")
        and last < datetime.fromisoformat(t["expiry_at"])
        for t in ticks
    )
