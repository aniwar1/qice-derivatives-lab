"""按交易所交易日归档原始日行情，记录缺失并导出可复核的合约数据。"""

import argparse
from collections import Counter, defaultdict
import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from akshare.futures.cons import get_calendar

from scripts.runtime_universe import canonical_delivery_contract


ROOT = Path(__file__).resolve().parents[1]
FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "reported_iv_percent",
)


def atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        file.write(payload)
    os.replace(file.name, path)


def store_json(path, value):
    atomic(
        path,
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode(),
    )


def request_spec(exchange, kind, day):
    stamp = day.replace("-", "")
    if exchange == "CZCE":
        name = kind.title()
        return (
            f"https://www.czce.com.cn/cn/DFSStaticFiles/{name}/{stamp[:4]}/{stamp}/{name}DataDaily.txt",
            {},
        )
    if exchange == "SHFE":
        return (
            f"https://www.shfe.com.cn/data/tradedata/{kind}/dailydata/kx{stamp}.dat",
            {},
        )
    if exchange == "DCE":
        return "https://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes", {
            "json": {
                "contractId": "",
                "lang": "zh",
                "optionSeries": "",
                "statisticsType": 0,
                "tradeDate": stamp,
                "tradeType": "1" if kind == "future" else "2",
                "varietyId": "all",
            }
        }
    return "https://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList", {
        "data": {
            "trade_date": stamp,
            "trade_type": "0" if kind == "future" else "1",
        }
    }


def raw_rows(exchange, kind, day, blob):
    if exchange == "CZCE":
        title, header, *lines = blob.decode("utf-8-sig").splitlines()
        if day not in title or ("期权" if kind == "option" else "期货") not in title:
            raise ValueError("交易日或品类不符")
        columns = [s.strip() for s in header.split("|")]
        for line in lines:
            row = dict(zip(columns, (s.strip() for s in line.split("|"))))
            yield (
                row.get("合约代码", ""),
                [
                    row.get(k)
                    for k in (
                        "今开盘",
                        "最高价",
                        "最低价",
                        "今收盘",
                        "成交量(手)",
                        "持仓量",
                        "隐含波动率",
                    )
                ],
            )
    elif exchange == "SHFE":
        data = json.loads(blob)
        if (int(data["o_month"]), int(data["o_day"])) != (int(day[5:7]), int(day[8:])):
            raise ValueError("交易日不符")
        for row in data["o_curinstrument"]:
            name = (
                row.get("INSTRUMENTID")
                if kind == "option"
                else str(row.get("PRODUCTGROUPID", "")).strip()
                + str(row.get("DELIVERYMONTH", "")).strip()
            )
            yield (
                name,
                [
                    row.get(k)
                    for k in (
                        "OPENPRICE",
                        "HIGHESTPRICE",
                        "LOWESTPRICE",
                        "CLOSEPRICE",
                        "VOLUME",
                        "OPENINTEREST",
                        "IMPLIEDVOLATILITY",
                    )
                ],
            )
    else:
        data = json.loads(blob)
        for row in data["data"]:
            trade_date = row.get("tradeDate") or row.get("trade_date")
            if trade_date and str(trade_date).replace("-", "")[:8] != day.replace(
                "-", ""
            ):
                raise ValueError("交易日不符")
            name = row.get("contractId") or row.get("delivMonth", "")
            if exchange == "GFEX" and kind == "future":
                name = str(row.get("varietyOrder", "")) + name
            yield (
                name,
                [
                    row.get(k)
                    for k in (
                        "open",
                        "high",
                        "low",
                        "close",
                        "volumn",
                        "openInterest",
                        "impliedVolatility",
                    )
                ],
            )


def parse(exchange, kind, day, raw, universe):
    result = {}
    counts = Counter()
    for name, fields in raw_rows(exchange, kind, day, raw):
        match = re.fullmatch(
            r"([A-Z]+)(\d{3,4})([CP]\d+(?:\.\d+)?)?",
            str(name).strip().replace("-", "").upper(),
        )
        if (
            not match
            or universe.get(match[1]) != exchange
            or bool(match[3]) != (kind == "option")
        ):
            continue
        contract = canonical_delivery_contract(
            match[1] + match[2],
            exchange=exchange,
            reference_date=date.fromisoformat(day),
            allow_czce_three_digit=True,
        )
        symbol = contract + (match[3] or "")
        if symbol in result:
            raise ValueError("同日合约重复")
        vals = []
        for item in fields:
            try:
                val = float(str(item).strip().replace(",", ""))
            except ValueError:
                val = None
            vals.append(val if val is not None and math.isfinite(val) else None)
        result[symbol] = vals
        counts[match[1]] += 1
    if not result:
        raise ValueError("没有可辨识的品种行情")
    return result, counts


def revision_metadata(exchange, day, blob, last_modified=None):
    disclosed = (
        parsedate_to_datetime(last_modified).astimezone(ZoneInfo("Asia/Shanghai"))
        if last_modified
        else None
    )
    if exchange != "SHFE":
        return {"disclosed_available_at": disclosed.isoformat()} if disclosed else {}
    raw = json.loads(blob)
    if raw.get("report_date") != day.replace("-", ""):
        raise ValueError("上期所原件完整交易日期不符")
    modified = datetime.strptime(raw["update_date"], "%Y%m%d %H:%M:%S").replace(
        tzinfo=ZoneInfo("Asia/Shanghai")
    )
    if modified.date() < date.fromisoformat(day):
        raise ValueError("上期所原件修订时间早于交易日")
    return {"disclosed_available_at": max(modified, disclosed or modified).isoformat()}


def collect(out, universe, day, exchange, kind):
    url, kwargs = request_spec(exchange, kind, day)
    stem = out / "raw" / exchange / f"{day}-{kind}"
    content = stem.with_suffix(".gz")
    receipt = stem.with_suffix(".json")
    if receipt.exists():
        row = json.loads(receipt.read_bytes())
        blob = gzip.decompress(content.read_bytes())
        if hashlib.sha256(blob).hexdigest() != row["sha256"]:
            raise ValueError("已归档原件摘要变化")
        return row | revision_metadata(exchange, day, blob, row.get("last_modified"))
    response = requests.request(
        "POST" if kwargs else "GET", url, **kwargs, timeout=(5, 18)
    )
    response.raise_for_status()
    if urlsplit(response.url).hostname != urlsplit(url).hostname:
        raise ValueError("发布主体变化")
    blob = response.content
    _, counts = parse(exchange, kind, day, blob, universe)
    row = {
        "day": day,
        "exchange": exchange,
        "type": kind,
        "https": url,
        "request": kwargs,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "raw": str(content.relative_to(out)),
        "configured_product_rows": dict(counts),
        "last_modified": response.headers.get("Last-Modified"),
        **revision_metadata(exchange, day, blob, response.headers.get("Last-Modified")),
    }
    atomic(content, gzip.compress(blob, compresslevel=2, mtime=0))
    store_json(receipt, row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--exchanges",
        nargs="+",
        choices=("SHFE", "CZCE", "GFEX", "DCE"),
        default=["SHFE", "CZCE", "GFEX", "DCE"],
    )
    parser.add_argument(
        "--gfex-history", type=Path, help="使用已锁定摘要的广期所期货日线历史快照补缺"
    )
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    out = args.out
    universe = {
        p["symbol"]: p["exchange"]
        for p in json.loads((ROOT / "config/trading_universe.json").read_text())[
            "permission_scope"
        ]["allowed_products"]
    }
    calendar = sorted(
        date.fromisoformat(day).isoformat()
        for day in get_calendar()
        if args.start <= date.fromisoformat(day) <= args.end
    )
    if not calendar or date.fromisoformat(get_calendar()[-1]) < args.end:
        raise ValueError("交易日历覆盖不足")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot_rows = defaultdict(dict)
    snapshot_receipts = {}
    if args.gfex_history:
        blob = args.gfex_history.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        expected = json.loads(
            (ROOT / "config/daily_history_integrity.json").read_text()
        )["anchor"]["sha256"]
        if digest != expected:
            raise ValueError("本机历史缓存与已发布摘要不符")
        raw_path = Path("raw") / "history" / f"{digest}.csv.gz"
        atomic(out / raw_path, blob)
        calendar_set = set(calendar)
        for row in csv.DictReader(io.StringIO(gzip.decompress(blob).decode())):
            day = row["date"]
            if (
                row["exchange"] == "GFEX"
                and day in calendar_set
                and universe.get(row["product"]) == "GFEX"
            ):
                snapshot_rows[day][row["contract"]] = [
                    float(row[k]) for k in FIELDS[:6]
                ] + [None]
        for day, values in snapshot_rows.items():
            counts = Counter(re.match(r"[A-Z]+", name)[0] for name in values)
            snapshot_receipts[day] = {
                "day": day,
                "exchange": "GFEX",
                "type": "future",
                "https": "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList",
                "sha256": digest,
                "raw": str(raw_path),
                "configured_product_rows": dict(counts),
                "origin": "已归档的交易所适配器日线快照",
                "vintage": "回溯取得，原始发布版本未核实",
            }
    jobs = [
        (day, ex, kind)
        for day in calendar
        for ex in args.exchanges
        for kind in ("future", "option")
    ]

    def run(job):
        day, ex, kind = job
        try:
            return collect(out, universe, day, ex, kind)
        except (ValueError, requests.RequestException, KeyError, OSError) as exc:
            return {"day": day, "exchange": ex, "type": kind, "error": str(exc)[:240]}

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        attempts = list(executor.map(run, jobs))
    good = [row for row in attempts if "sha256" in row]
    gaps = [row for row in attempts if "error" in row]
    direct_keys = {(r["day"], r["exchange"], r["type"]) for r in good}
    good.extend(
        r
        for day, r in snapshot_receipts.items()
        if (day, "GFEX", "future") not in direct_keys
    )
    covered = {(r["day"], r["exchange"], r["type"]) for r in good}
    skipped = [
        {"day": d, "exchange": ex, "type": k, "error": "本轮未取得可验证的交易所日行情"}
        for d in calendar
        for ex in set(universe.values()) - set(args.exchanges)
        for k in ("future", "option")
        if (d, ex, k) not in covered
    ]
    gaps.extend(skipped)
    store_json(out / "attempts.json", attempts)
    with tempfile.NamedTemporaryFile(dir=out, delete=False) as target:
        with gzip.GzipFile(
            filename="", fileobj=target, mode="wb", compresslevel=3, mtime=0
        ) as zipped:
            zipped.write(b"{")
            for idx, day in enumerate(calendar):
                market = {"future": {}, "option": {}}
                for row in good:
                    if row["day"] == day:
                        if row.get("origin"):
                            fields = snapshot_rows[day]
                        else:
                            fields, _ = parse(
                                row["exchange"],
                                row["type"],
                                day,
                                gzip.decompress((out / row["raw"]).read_bytes()),
                                universe,
                            )
                        market[row["type"]].update(fields)
                zipped.write(
                    (
                        ("," if idx else "")
                        + json.dumps(day)
                        + ":"
                        + json.dumps(market, separators=(",", ":"))
                    ).encode()
                )
            zipped.write(b"}")
    final = out / "market.json.gz"
    os.replace(target.name, final)
    store_json(
        out / "source-manifest.json",
        {
            "configured_universe": universe,
            "calendar": calendar,
            "sources": good,
            "gaps": gaps,
            "fields": FIELDS,
            "data_sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
            "vintage": "依据交易日重建，交易所文件为本次下载版本，逐日原始发布版本未证实。",
        },
    )
    print("sessions", len(calendar), "files", len(good), "gaps", len(gaps), flush=True)


if __name__ == "__main__":
    main()
