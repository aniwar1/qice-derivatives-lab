"""按实际合约读取交易所会员排行，保留所有权边界。"""
from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256

import requests

from .live_options import BJT
from .models import PRODUCT_EXCHANGES
from .research_archive import MarketObservation


def collect_member_positions(observations: list[MarketObservation]) -> tuple[dict, dict[str, bytes]]:
    groups: dict[date, list] = {}
    items, blobs, gaps = [], {}, {}
    for row in observations:
        if PRODUCT_EXCHANGES[row.product] != "SHFE":
            gaps[row.product] = "本轮渠道无可核实的该合约会员席位榜"
            continue
        days = [date.fromisoformat(bar["date"]) for bar in row.daily_bars if bar.get("date")]
        if days:
            groups.setdefault(max(days), []).append(row)
        else:
            gaps[row.product] = "缺少已完成交易日"
    for day, positions in groups.items():
        url = f"https://www.shfe.com.cn/data/tradedata/future/dailydata/pm{day:%Y%m%d}.dat"
        try:
            response = requests.get(url, timeout=(2, 4), allow_redirects=False)
            response.raise_for_status()
            payload = response.json()
            stamp = datetime.strptime(payload["update_date"].strip(), "%Y%m%d %H:%M:%S").replace(tzinfo=BJT)
            retrieved = datetime.now(BJT)
            if payload["report_date"].strip() != day.strftime("%Y%m%d") or stamp > retrieved:
                raise ValueError("交易日期或公告时点不符")
            digest = sha256(response.content).hexdigest()
            for row in positions:
                ranks = []
                for line in payload["o_cursor"]:
                    if line["INSTRUMENTID"].strip().upper() != row.contract or not 1 <= int(line["RANK"]) <= 20:
                        continue
                    ranks.append({"rank": int(line["RANK"]), "long_member": line["PARTICIPANTABBR2"].strip(),
                                  "long_lots": line["CJ2"], "short_member": line["PARTICIPANTABBR3"].strip(),
                                  "short_lots": line["CJ3"]})
                if not ranks:
                    gaps[row.product] = "交易所当日榜单缺少该合约"
                    continue
                items.append({"id": f"{row.contract}:{digest}", "product": row.product, "contract": row.contract,
                              "trade_date": day.isoformat(), "published_at": stamp.isoformat(),
                              "available_at": retrieved.isoformat(), "url": url, "sha256": digest,
                              "basis": "exchange_member_aggregate", "ownership": "unknown", "ranks": ranks})
                blobs[digest] = response.content
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            for row in positions:
                gaps[row.product] = "交易所榜单不可用或日期身份校验失败"
    return {"items": items, "gaps": gaps}, blobs
