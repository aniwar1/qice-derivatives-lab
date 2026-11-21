"""采集同一标的月份的期权报价，区分采集时间、报价时间和盘口深度。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from hashlib import sha256
import math
import re
from zoneinfo import ZoneInfo

import requests

from .models import PRODUCT_EXCHANGES
from .research_archive import MarketObservation

BJT = ZoneInfo("Asia/Shanghai")
CHAIN_URL = "https://stock.finance.sina.com.cn/futures/api/openapi.php/OptionService.getOptionData"
TICK_URL = "https://hq.sinajs.cn/list="
QUOTE_LINE = re.compile(r'var\s+hq_str_P_OP_([A-Za-z0-9.\-]+)="([^"]*)";')
OPTION_SYMBOL = re.compile(r"([A-Z]+)(\d{3,4})([CP])(\d+(?:\.\d+)?)")


def _identity(symbol: str, underlying: str, exchange: str):
    match = OPTION_SYMBOL.fullmatch(symbol.upper().replace("-", ""))
    if not match:
        return None
    product, month, side, strike = match.groups()
    canonical = underlying[len(product):]
    if not underlying.startswith(product) or not (month == canonical or (
        exchange == "CZCE" and len(month) == 3 and month == canonical[1:]
    )):
        return None
    return side, float(strike)


def _quotes(observation: MarketObservation) -> tuple[list[dict], str | None, dict[str, bytes]]:
    contract = observation.contract
    exchange = PRODUCT_EXCHANGES[observation.product]
    prefix = observation.product.lower()
    try:
        with requests.Session() as session:
            session.headers["Referer"] = "https://finance.sina.com.cn/"
            response = session.get(CHAIN_URL, params={
                "type": "futures", "product": prefix if exchange == "CZCE" else prefix + "_o",
                "exchange": exchange.lower(), "pinzhong": contract.lower(),
            }, timeout=(2, 3))
            response.raise_for_status()
            chain = response.json()["result"]["data"]
            selected = []
            for key, side in (("up", "C"), ("down", "P")):
                ranked = []
                for row in chain.get(key, []):
                    symbol = row[-1]
                    identity = _identity(symbol, contract, exchange)
                    if identity is None or identity[0] != side:
                        continue
                    distance = identity[1] - observation.price
                    if distance * (1 if side == "C" else -1) >= 0:
                        ranked.append((abs(distance), symbol))
                selected.extend(symbol for _, symbol in sorted(ranked)[:5])
            if not selected:
                return [], "未找到对应实际月份的可识别期权链", {}
            tick = session.get(TICK_URL + ",".join("P_OP_" + name for name in selected), timeout=(2, 3))
            tick.raise_for_status()
            digest = sha256(tick.content).hexdigest()
            fetched = datetime.now(BJT)
            items = []
            for symbol, fields in QUOTE_LINE.findall(tick.content.decode("gb18030")):
                if symbol not in selected:
                    continue
                try:
                    row = fields.split(",")
                    side, strike = _identity(symbol, contract, exchange)
                    at = datetime.strptime(row[32], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
                    bid_size, bid, ask, ask_size = int(row[0]), float(row[1]), float(row[3]), int(row[4])
                    if not (at <= fetched <= at + timedelta(minutes=2) and 0 < bid <= ask
                            and math.isfinite(ask) and min(bid_size, ask_size) > 0 and strike == float(row[7])):
                        continue
                except (IndexError, TypeError, ValueError):
                    continue
                items.append({"symbol": symbol, "contract": contract, "side": side, "strike": strike,
                              "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
                              "observed_at": at.isoformat(), "available_at": fetched.isoformat(),
                              "source_url": tick.url, "sha256": digest})
            return items, (None if items else "缺少两分钟内的有效双边盘口"), ({digest: tick.content} if items else {})
    except (requests.RequestException, UnicodeError, ValueError, TypeError, AttributeError, IndexError, KeyError):
        return [], "期权行情源不可用或报价格式无法核验", {}


def collect_option_books(observations: list[MarketObservation]) -> tuple[dict, dict[str, bytes]]:
    now = datetime.now(BJT)
    recent = [row for row in observations if now - timedelta(minutes=30) <= row.observed_at <= now]
    gaps = {row.product: "标的报价已过时，本轮未采集期权盘口" for row in observations if row not in recent}
    books, blobs = [], {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, (ticks, gap, originals) in zip(recent, pool.map(_quotes, recent)):
            books.extend(ticks)
            blobs.update(originals)
            if gap:
                gaps[row.product] = gap
    return {"books": books, "gaps": gaps}, blobs
