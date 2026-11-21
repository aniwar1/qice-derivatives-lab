"""保存中期协每期原始统计表，按该期发布日期索引品种月度市场统计。"""

import argparse
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from openpyxl import load_workbook
import requests

from scripts.collect_replay_market import ROOT, atomic, store_json

INDEX = "https://www.cfachina.org/servicesupport/researchandpublishin/statisticalsdata/monthlytransactiondata/"


def official_url(base, href):
    url = urljoin(base, href)
    if urlsplit(url).scheme != "https" or urlsplit(url).hostname != "www.cfachina.org":
        raise ValueError("统计附件发布域名错误")
    return url


def available_at(published, *modifications):
    zone = ZoneInfo("Asia/Shanghai")
    first = datetime.combine(published + timedelta(days=1), time.min, zone)
    return max(
        [first]
        + [parsedate_to_datetime(h).astimezone(zone) for h in modifications if h]
    )


def issues(get, start, end):
    page_no = 0
    while True:
        url = INDEX if page_no == 0 else urljoin(INDEX, f"index_{page_no}.html")
        raw, _ = get(url)
        soup = BeautifulSoup(raw, "html.parser", from_encoding="utf-8")
        dates = []
        for link in soup.select("a[href]"):
            match = re.fullmatch(
                r"(\d{4})年(\d{1,2})月全国期货市场交易情况", link.get_text(strip=True)
            )
            if not match:
                continue
            article = official_url(url, link["href"])
            stamp = re.search(r"/t(\d{8})_\d+\.html$", article)
            if stamp is None:
                raise ValueError("无法核实中期协发布日期")
            published = date.fromisoformat(stamp[1])
            dates.append(published)
            if start <= published <= end:
                yield int(match[1]), int(match[2]), published, article
        if not dates:
            raise ValueError("中期协目录为空")
        pagination = re.search(rb"createPageHTML\((\d+),\s*\d+,", raw)
        page_no += 1
        if min(dates) < start or not pagination or page_no >= int(pagination[1]):
            break


def collect(out, start, end):
    specs = json.loads((ROOT / "config/product_contract_specs.json").read_text())[
        "products"
    ]
    aliases = {row["name"].split("（")[0]: row["symbol"] for row in specs}
    aliases.update({"一号棉": "CF", "天胶": "RU", "聚氯乙烯": "V"})

    def get(url):
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        official_url(url, response.url)
        return response.content, response.headers.get("Last-Modified")

    records = []
    for year, month, published, page_url in issues(get, start, end):
        period = f"{year}-{month:02d}"
        page, page_modified = get(page_url)
        soup = BeautifulSoup(page, "html.parser", from_encoding="utf-8")
        anchor = next(
            a
            for a in soup.select("a[href]")
            if a["href"].lower().endswith(".xlsx") and "统计表" in a.get_text()
        )
        source_url = official_url(page_url, anchor["href"])
        raw, file_modified = get(source_url)
        available = available_at(published, page_modified, file_modified)
        digest = sha256(raw).hexdigest()
        atomic(out / "originals" / f"{digest}.xlsx", raw)
        atomic(out / "originals" / f"{sha256(page).hexdigest()}.html", page)
        sheet = load_workbook(BytesIO(raw), read_only=True, data_only=True)[
            f"{month}月"
        ]
        rows = iter(sheet.values)
        next(rows)
        next(rows)
        header = next(rows)
        if "成交量" not in header[2] or "月末持仓量" not in header[22]:
            raise ValueError("统计表列发生变化")
        for row in rows:
            label = str(row[1] or "").replace(" ", "")
            name = re.sub(r"(期货|期权|[A-Z]+)$", "", label)
            symbol = aliases.get(name)
            if not symbol or not isinstance(row[2], (int, float)):
                continue
            records.append(
                {
                    "product": symbol,
                    "month": period,
                    "kind": "option" if "期权" in label else "future",
                    "volume_lots": row[2],
                    "month_end_open_interest": row[22],
                    "available_at": available.isoformat(),
                    "published_day": published.isoformat(),
                    "page_last_modified": page_modified,
                    "file_last_modified": file_modified,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "page_url": page_url,
                    "source_url": source_url,
                    "sha256": digest,
                    "page_sha256": sha256(page).hexdigest(),
                }
            )
    records.sort(key=lambda item: (item["month"], item["product"], item["kind"]))
    if not records or len(
        {(r["month"], r["product"], r["kind"]) for r in records}
    ) != len(records):
        raise ValueError("统计记录为空或品种重复")
    store_json(out / "index.json", records)
    print(
        len(records),
        len({r["product"] for r in records}),
        sorted({r["month"] for r in records}),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    collect(args.out, args.start, args.end)
