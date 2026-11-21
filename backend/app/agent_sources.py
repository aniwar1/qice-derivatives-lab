"""按资料公开时点构建研究上下文，仅采集白名单发布者的原始资料。"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import urljoin, urlsplit
from zipfile import BadZipFile
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from openpyxl import load_workbook
import requests


CN = ZoneInfo("Asia/Shanghai")
NBS = "https://www.stats.gov.cn/sj/zxfb/"
SHFE_RULE = "https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/?query_params=pm"
CALENDAR = {2026: "https://www.stats.gov.cn/xw/tjxw/tzgg/202512/t20251224_1962137.html"}
CHANNELS = {
    "www.stats.gov.cn": "国家统计局",
    "www.czce.com.cn": "郑州商品交易所",
    "www.shfe.com.cn": "上海期货交易所",
    "xmsyj.moa.gov.cn": "农业农村部畜牧兽医局",
    "scs.moa.gov.cn": "农业农村部市场预警专家委员会",
}
PRODUCTS = "AP CF CJ CU FG JD JM LC LH M MA OI PK PS RU SA SI SM SP SR UR V".split()
INDUSTRIAL = "CU FG JM LC MA PS RU SA SI SM SP UR V".split()
AGRI = "AP CF CJ JD LH M OI PK SR".split()
POSITIONING = {
    "classification": "unknown",
    "exchange_membership_is_proprietary_evidence": False,
    "reference_url": SHFE_RULE,
    "rule": "经纪会员排名仅作席位或代客口径，非期货公司会员汇总亦不能识别单一机构。具名主体、合约、报告时点及明确自有账户披露须同时核实，方可使用机构自营口径。仓单度量可交割库存。",
}
SOURCE_POLICY = {
    "primary_official": "主管部门和交易所在职责范围内的原始统计可作依据。发行人在指定披露平台公布的经营数据只描述该企业，并注明报告期，仅作低频背景。",
    "secondary": "媒体转述仅作寻找原始公告的线索，转载共享同一原件。",
    "unverified": "传闻、自媒体与不能追溯原件的消息不进入决策依据。",
}


def _stamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("研究截止时刻必须携带时区")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _write(path: Path, data: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        try:
            f.write(data)
            f.close()
            os.replace(f.name, path)
        finally:
            Path(f.name).unlink(missing_ok=True)


def _issuer(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in CHANNELS:
        raise ValueError("不在直接采集的官方发布渠道中")
    return CHANNELS[parsed.netloc]


def _fetch(root: Path, url: str, *, reuse: bool):
    """保存原文和取得时间；日期固定的历史文件允许复用已校验原件。"""
    _issuer(url)
    receipt = root / "retrievals" / (sha256(url.encode()).hexdigest() + ".json")
    if reuse and receipt.exists():
        stored = json.loads(receipt.read_bytes())
        raw = (root / "blobs" / stored["sha256"]).read_bytes()
        if sha256(raw).hexdigest() != stored["sha256"]:
            raise ValueError("归档原件摘要不符")
        return raw, stored
    with requests.get(url, timeout=(5, 12), allow_redirects=False, stream=True) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("未获得官方原始文件")
        raw = bytearray()
        for block in response.iter_content(131072):
            raw.extend(block)
            if len(raw) > 6_000_000:
                raise ValueError("原件超出当前渠道文件大小")
        data = bytes(raw)
        raw_digest = sha256(data).hexdigest()
        stored = {"url": url, "retrieved_at": _stamp(datetime.now(timezone.utc)),
                  "sha256": raw_digest, "last_modified": response.headers.get("Last-Modified")}
    blob = root / "blobs" / raw_digest
    if not blob.exists():
        _write(blob, data)
    _write(receipt, json.dumps(stored).encode())
    return data, stored


def _warehouse_jobs(start: date, end: date):
    day = start
    while day <= end:
        if day.weekday() < 5:
            key = day.strftime("%Y%m%d")
            yield "czce", day, f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day.year}/{key}/FutureDataWhsheet.xlsx"
            yield "shfe", day, f"https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_{key}/ZH/all.html"
        day += timedelta(days=1)


def _nbs_listing(root: Path, start: date, end: date, pages: int):
    seen = set()
    for page in range(pages):
        url = NBS + (f"index_{page}.html" if page else "index.html")
        raw, _ = _fetch(root, url, reuse=False)
        soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser")
        page_days = []
        for link in soup.select("a[href]"):
            article = urljoin(url, link["href"])
            match = re.fullmatch(re.escape(NBS) + r"\d{6}/t(\d{8})_\d+\.html", article)
            if not match:
                continue
            day = datetime.strptime(match[1], "%Y%m%d").date()
            page_days.append(day)
            title = link.get("title") or link.get_text(strip=True)
            if (article not in seen and start - timedelta(days=4) <= day <= end
                    and re.search("流通领域重要生产资料市场价格|规模以上工业增加值|房地产市场基本情况|采购经理指数运行情况|居民消费价格|工业生产者出厂价格", title)):
                seen.add(article)
                yield "nbs", day, article
        if not page_days:
            raise ValueError("国家统计局目录没有可解析的日期链接")
        if min(page_days) < start - timedelta(days=4):
            break


def _fact(products, dimension, scope, title, excerpt, published, *, precision="minute", scheduled=None):
    return {"products": products, "dimension": dimension, "scope": scope, "title": title,
            "excerpt": excerpt, "published_at": _stamp(published), "time_precision": precision,
            "scheduled_at": _stamp(scheduled) if scheduled is not None else None}


def _daily_release(day):
    return datetime.combine(day + timedelta(days=1), time.min, CN)


def _czce(raw, day):
    book = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    try:
        rows = iter(book.active.values)
        title = str(next(rows)[0])
        if day.isoformat() not in title or "仓单日报" not in title:
            raise ValueError("郑商所仓单日期或表名无法核对")
        product = None
        columns = {}
        facts = []
        for row in rows:
            name = str(row[0]).strip()
            if name.startswith("品种"):
                m = re.search(r"品种[：:].*?([A-Z]{1,3})\s+单位", name)
                product = m[1] if m else None
                columns = {}
            elif name in {"仓库编号", "厂库编号", "机构编号"}:
                columns = {str(k).strip(): i for i, k in enumerate(row) if k}
            elif name == "总计" and product in PRODUCTS:
                for heading, i in columns.items():
                    if not (heading.startswith("仓单数量") or heading == "预报数量"):
                        continue
                    quantity = row[i]
                    if isinstance(quantity, (float, int)):
                        facts.append(_fact([product], "fundamentals", "direct", f"{product} 交易所{heading}",
                                           f"{day} 郑商所公布{product}{heading}总计 {quantity:g} 张。",
                                           _daily_release(day), precision="date_upper_bound"))
    finally:
        book.close()
    return facts


def _shfe(raw, day):
    soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser")
    rows = soup.select("tr")
    if not rows or day.isoformat() not in rows[0].get_text():
        raise ValueError("上期所仓单日期无法核对")
    names = {"铜": "CU", "天然橡胶": "RU", "纸浆(仓库)": "SP", "纸浆(厂库)": "SP"}
    subject = ""
    facts = []
    for row in rows:
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td", recursive=False)]
        if not cells:
            continue
        line = " ".join(cells)
        if "单位" in line:
            subject = line.split("单位")[0].strip()
        elif cells[0] == "总计" and subject in names and re.fullmatch(r"-?[\d,]+", cells[-2]):
            facts.append(_fact([names[subject]], "fundamentals", "direct", f"{subject}交易所仓单",
                               f"{day} 上期所{subject}注册仓单 {cells[-2]} 吨，较上日增减 {cells[-1]} 吨。",
                               _daily_release(day), precision="date_upper_bound"))
            subject = ""
    return facts


# 直接品名或明确的产业链传导项。代理指标的用途在每条资料中保留。
SPOT_ROWS = {
    "电解铜": ("CU", "direct"), "甲醇": ("MA", "direct"), "焦煤": ("JM", "direct"),
    "浮法平板玻璃": ("FG SA", "mixed"), "多晶硅": ("PS SI", "mixed"),
    "生猪": ("LH", "direct"), "棉花": ("CF", "direct"), "豆粕": ("M JD", "mixed"),
    "白糖": ("SR", "direct"), "花生": ("PK", "direct"), "尿素": ("UR", "direct"),
    "天然橡胶": ("RU", "direct"), "纸浆": ("SP", "direct"), "磷酸铁锂": ("LC", "supply_chain"),
}
INDUSTRY_ROWS = {
    "粗钢": "JM SM", "平板玻璃": "FG SA", "十种有色金属": "CU", "汽车": "RU",
    "新能源汽车": "LC", "发电设备": "CU", "太阳能电池": "PS SI",
    "农副食品加工业": "OI M", "纺织业": "CF", "橡胶和塑料制品业": "RU V",
}
CPI_ROWS = {"蛋类": "JD", "鲜果": "AP CJ", "猪肉": "LH", "食用油": "OI PK"}
CALENDAR_PRODUCTS = {
    "规模以上工业生产月度报告": INDUSTRIAL,
    "房地产开发和销售情况月度报告": "FG SA V SM JM CU".split(),
    "居民消费价格指数月度报告": AGRI,
    "工业生产者价格指数月度报告": INDUSTRIAL,
}


def _clean(value):
    return " ".join(value.split()).replace("\u3000", " ")


def _nbs(raw, day, calendar=False):
    soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser")
    header = soup.find("meta", attrs={"name": "ArticleTitle"})
    pub = soup.find("meta", attrs={"name": "PubDate"})
    section = soup.select_one(".txt-content")
    if not header or not pub or not section:
        raise ValueError("国家统计局原始发布日期或正文缺失")
    title = header["content"]
    if any(flag in title for flag in ("修订", "更正", "勘误")):
        raise ValueError("修订资料须单独核实更正发布时间")
    posted = datetime.strptime(pub["content"], "%Y/%m/%d %H:%M").replace(tzinfo=CN)
    table = section.find("table")
    rows = table.select("tr") if table is not None else []
    if calendar:
        year = int(title[:4])
        if posted.year >= year or "主要统计信息发布日程表" not in title:
            raise ValueError("发布日程缺少事前年度依据")
        events = []
        for i, row in enumerate(rows[:-1]):
            cells = row.find_all("td", recursive=False)
            if len(cells) != 14:
                continue
            label = cells[1].get_text(strip=True)
            if label not in CALENDAR_PRODUCTS:
                continue
            times = iter(rows[i+1].find_all("td", recursive=False))
            for month, cell in enumerate(cells[2:], start=1):
                if cell.get("rowspan") == "2":
                    continue
                clock = next(times).get_text(strip=True)
                day_match = re.fullmatch(r"(\d{1,2})/[一二三四五六日]", cell.get_text(strip=True))
                if day_match and re.fullmatch(r"\d{1,2}:\d{2}", clock):
                    hh, mm = map(int, clock.split(":"))
                    event_time = datetime(year, month, int(day_match[1]), hh, mm, tzinfo=CN)
                    events.append(_fact(CALENDAR_PRODUCTS[label], "news", "sector_calendar",
                                        label, f"国家统计局原定于{event_time:%Y-%m-%d %H:%M}北京时间发布{label}。此条仅含年度预定日程。",
                                        posted, scheduled=event_time))
        return events
    if abs((posted.date() - day).days) > 4:
        raise ValueError("国家统计局页面时点与地址日期不一致")
    if "流通领域重要生产资料市场价格" in title:
        table_kind = "spot"
    elif "工业增加值" in title:
        table_kind = "production"
    elif "居民消费价格" in title:
        table_kind = "consumer"
    else:
        body = section.get_text(" ", strip=True)
        if "房地产市场基本情况" in title:
            syms = CALENDAR_PRODUCTS["房地产开发和销售情况月度报告"]
        elif "工业生产者出厂价格" in title or "采购经理指数" in title:
            syms = INDUSTRIAL
        else:
            return []
        return [_fact(syms, "news", "sector_context", title, _clean(body)[:270], posted)]
    found = []
    for tr in rows:
        cells = ["".join(td.stripped_strings) for td in tr.find_all("td", recursive=False)]
        if len(cells) < 4 or not any(re.search(r"\d", c) for c in cells[1:]):
            continue
        label = re.sub(r"\s+", "", cells[0]).replace("其中：", "")
        if table_kind == "spot":
            code = next((v for k,v in SPOT_ROWS.items() if label.startswith(k)), None)
            if not code:
                continue
            syms, scope = code
            fact = f"{title}。{label}，单位{cells[1]}，本期{cells[2]}元，较上期变动{cells[3]}元，幅度{cells[4]}%。"
        else:
            mapping = INDUSTRY_ROWS if table_kind == "production" else CPI_ROWS
            key = next((k for k in mapping if label == k or label.startswith(k + "（")), None)
            if not key:
                continue
            syms, scope = mapping[key], "supply_chain"
            column_rows = [" ".join(c.get_text(strip=True) for c in h.select("td")) for h in rows[:2]]
            fact = f"{title}；栏目 {' / '.join(column_rows)}；行 {label}：{' / '.join(cells[1:])}"
        products = syms.split()
        if scope == "mixed":
            found.append(_fact(products[:1], "fundamentals", "direct", f"{title} · {label}", fact, posted))
            found.append(_fact(products[1:], "fundamentals", "supply_chain", f"{title} · {label}", fact, posted))
        else:
            found.append(_fact(products, "fundamentals", scope, f"{title} · {label}", fact, posted))
    return found


def _db(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(root / "facts.sqlite3", timeout=20)
    db.execute("""CREATE TABLE IF NOT EXISTS facts(
        fact_id TEXT PRIMARY KEY, fact_key TEXT NOT NULL, available TEXT NOT NULL, scheduled TEXT,
        symbol TEXT NOT NULL, dimension TEXT NOT NULL, family TEXT NOT NULL, data TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS facts_lookup ON facts(symbol,available,scheduled)")
    return db


def _store(root, kind, receipt, facts):
    published_origin = _issuer(receipt["url"])
    observed = datetime.fromisoformat(receipt["retrieved_at"])
    try:
        modified = parsedate_to_datetime(receipt["last_modified"]) if receipt["last_modified"] else None
    except (TypeError, ValueError):
        modified = None
    db = _db(root)
    try:
        with db:
            # 同一地址出现新版本时，新出现的标题和品种也不能倒填到原发布日期。
            at_url = db.execute("SELECT 1 FROM facts WHERE json_extract(data,'$.url')=? AND json_extract(data,'$.sha256')<>? LIMIT 1",
                                (receipt["url"], receipt["sha256"])).fetchone()
            for fact in facts:
                official = datetime.fromisoformat(fact["published_at"])
                stamp = max(official, modified) if modified and modified.utcoffset() is not None else official
                for product in fact["products"]:
                    key = json.dumps([receipt["url"], product, fact["title"], fact["scheduled_at"]], ensure_ascii=False)
                    prev = db.execute("SELECT data FROM facts WHERE fact_key=? ORDER BY available DESC LIMIT 1", [key]).fetchone()
                    if prev:
                        saved = json.loads(prev[0])
                        if (saved["excerpt"], saved["published_at"]) == (fact["excerpt"], fact["published_at"]):
                            continue
                        availability = max(observed, stamp)
                        provenance = "observed_revision"
                    elif at_url:
                        availability = max(observed, stamp)
                        provenance = "observed_revision"
                    else:
                        availability = stamp
                        provenance = "reconstructed_official_release"
                    available_at = _stamp(availability)
                    fact_id = sha256((key + receipt["sha256"] + available_at).encode()).hexdigest()
                    item = {**fact, "products": [product], "id": fact_id, "available_at": available_at,
                            "authority": "primary_official", "publisher": published_origin,
                            "vintage": provenance, "channel": kind, **receipt}
                    db.execute("INSERT OR IGNORE INTO facts VALUES(?,?,?,?,?,?,?,?)",
                               [fact_id, key, available_at, fact["scheduled_at"], product, fact["dimension"], kind,
                                json.dumps(item, ensure_ascii=False)])
    finally:
        db.close()


def _moa(raw, day):
    soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser")
    section = soup.select_one(".sj_arc_body .TRS_Editor") or soup.select_one(".TRS_Editor")
    if not section:
        raise ValueError("农业农村部原稿缺少正文")
    published = soup.find("meta", attrs={"name": "PubDate"})
    title_tag = soup.find("meta", attrs={"name": "ArticleTitle"})
    if not published or not title_tag:
        raise ValueError("农业农村部缺少官方发布时间")
    posted = datetime.fromisoformat(published["content"]).replace(tzinfo=CN)
    title = title_tag["content"]
    if posted.date() != day or any(word in title for word in ("修订", "勘误", "更正")):
        raise ValueError("农业农村部发布日期或版本无法核实")
    paragraphs = [p.get_text("", strip=True).replace("\u3000", "") for p in section.select("p")]
    facts = []
    for p in paragraphs:
        if "畜产品和饲料" in title:
            for label, product in (("生猪", "LH"), ("鸡蛋", "JD"), ("豆粕", "M")):
                m = re.search(r"全国" + label + r"平均价格[0-9.]+元/公斤[^。]*。", p)
                if m:
                    facts.append(_fact([product], "fundamentals", "direct", title + " · " + label, m[0], posted))
        elif "中国农产品供需形势分析" in title:
            for label, products, scope in (("大豆", ["M"], "supply_chain"), ("棉花", ["CF"], "direct"),
                                            ("食用植物油", ["OI", "PK"], "supply_chain"), ("食糖", ["SR"], "direct")):
                if p.startswith(label+"：") and len(p) > 35 and "E-Mail" not in p:
                    facts.append(_fact(products, "fundamentals", scope, title+" · "+label, p[:270], posted))
    return facts


def _moa_listing(root, host, start, end, pages):
    base = "https://" + host + "/jcyj/"
    for i in range(pages):
        raw, _ = _fetch(root, base + ("index.htm" if not i else f"index_{i}.htm"), reuse=False)
        soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser")
        days = []
        for a in soup.select("a[href]"):
            url = urljoin(base, a["href"])
            m = re.fullmatch(re.escape(base) + r"\d{6}/t(\d{8})_\d+\.htm", url)
            if not m:
                continue
            day = datetime.strptime(m[1], "%Y%m%d").date()
            days.append(day)
            if start <= day <= end and re.search("畜产品和饲料集贸市场价格|中国农产品供需形势分析", a.get_text(strip=True)):
                yield "moa", day, url
        if not days or min(days) < start:
            break


def _archived_article_jobs(root, start, end):
    for entry in (root / "retrievals").glob("*.json"):
        url = json.loads(entry.read_bytes())["url"]
        m = re.fullmatch(r"https://(www.stats.gov.cn/sj/zxfb|(?:scs|xmsyj).moa.gov.cn/jcyj)/\d{6}/t(\d{8})_\d+\.html?", url)
        if m:
            day = datetime.strptime(m[2], "%Y%m%d").date()
            if start - timedelta(days=4) <= day <= end:
                yield "nbs" if url.startswith(NBS) else "moa", day, url


def _ingest(root, job, reuse):
    kind, day, url = job
    try:
        raw, receipt = _fetch(root, url, reuse=reuse)
        if kind == "czce":
            facts = _czce(raw, day)
        elif kind == "shfe":
            facts = _shfe(raw, day)
        elif kind == "moa":
            facts = _moa(raw, day)
        else:
            facts = _nbs(raw, day, calendar=kind == "calendar")
        if not facts:
            raise ValueError("官方原件没有可映射的品种事实")
        _store(root, kind, receipt, facts)
        return {"channel": kind, "date": day.isoformat(), "url": url, "status": "ok", "count": len(facts)}
    except (OSError, ValueError, requests.RequestException, KeyError, StopIteration) as exc:
        return {"channel": kind, "date": day.isoformat(), "url": url, "status": "gap", "reason": str(exc)[:170]}


def collect_range(*, archive_root: Path, start: date, end: date, live: bool = False) -> list[dict]:
    """历史回补按发布者的日期固定原件重建，并保留当前抓取时间。"""
    if end < start or end > datetime.now(CN).date():
        raise ValueError("采集窗口无效")
    jobs = list(_warehouse_jobs(start, end))
    statuses = []
    for year in range(start.year, end.year + 1):
        if year in CALENDAR:
            jobs.append(("calendar", date(year, 1, 1), CALENDAR[year]))
    pages = 2 if live else max(3, (end-start).days // 6 + 5)
    try:
        jobs.extend(_nbs_listing(archive_root, start, end, pages))
    except (requests.RequestException, ValueError, OSError) as exc:
        statuses.append({"channel": "nbs", "status": "gap", "reason": str(exc)[:170]})
    for host in ("xmsyj.moa.gov.cn", "scs.moa.gov.cn"):
        try:
            jobs.extend(_moa_listing(archive_root, host, start, end, 1 if live else pages))
        except (requests.RequestException, ValueError, OSError) as exc:
            statuses.append({"channel": host, "status": "gap", "reason": str(exc)[:170]})
    jobs.extend(_archived_article_jobs(archive_root, start, end))
    jobs = list(dict.fromkeys(jobs))

    def collect(job):
        try:
            return _ingest(archive_root, job, reuse=not live)
        except BadZipFile:
            return {"channel": job[0], "date": job[1].isoformat(), "url": job[2],
                    "status": "gap", "reason": "官方接口未返回有效 XLSX 文件"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses.extend(pool.map(collect, jobs))
    return statuses


@lru_cache(maxsize=4096)
def _raw_verified(path: Path, digest: str, size: int, mtime: int):
    return sha256(path.read_bytes()).hexdigest() == digest


def lookup_agent_sources(products: list[str], *, archive_root: Path, cutoff: datetime) -> dict:
    """只提供研究时点可见的事实，分别保留当前取得与官方归档重建的时间。"""
    as_of = _stamp(cutoff)
    requested = list(dict.fromkeys(products))
    if any(symbol not in PRODUCTS for symbol in requested):
        raise ValueError("未配置的研究品种")
    earliest = _stamp(cutoff - timedelta(days=45))
    latest_event = _stamp(cutoff + timedelta(days=7))
    from .agent_source_supplement import visible_items

    supplement = visible_items(archive_root, cutoff)
    selected, gaps = [], []
    path = archive_root / "facts.sqlite3"
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) if path.exists() else None
    try:
        for product in requested:
            rows = conn.execute("""
                SELECT data, fact_key FROM facts
                WHERE symbol=? AND available<=? AND
                  ((scheduled IS NULL AND available>=?) OR (scheduled>? AND scheduled<=?))
                ORDER BY available DESC, scheduled ASC, fact_id
                """, [product, as_of, earliest, as_of, latest_event]) if conn else ()
            seen = set()
            per_family = {}
            extra = supplement.get(product, [])
            selected.extend(extra)
            has = {"fundamentals", "direct_fundamental"} if extra else set()
            for data, key in rows:
                if key in seen:
                    continue
                seen.add(key)
                item = json.loads(data)
                group = (item["channel"], item["dimension"], item["scope"])
                if per_family.get(group, 0) >= (1 if item["scheduled_at"] else 2):
                    continue
                raw = archive_root / "blobs" / item["sha256"]
                if not raw.is_file():
                    continue
                stat = raw.stat()
                if not _raw_verified(raw, item["sha256"], stat.st_size, stat.st_mtime_ns):
                    continue
                per_family[group] = per_family.get(group, 0) + 1
                has.add(item["dimension"])
                if item["dimension"] == "fundamentals" and item["scope"] == "direct":
                    has.add("direct_fundamental")
                if item["dimension"] == "news" and not item["scheduled_at"]:
                    has.add("published_news")
                selected.append(item)
            for dimension in ("fundamentals", "news", "direct_fundamental", "published_news"):
                if dimension not in has:
                    gaps.append({"product": product, "dimension": dimension, "reason": "时点内无可复核的权威原件"})
    finally:
        if conn:
            conn.close()
    return {"as_of": as_of, "items": selected, "gaps": gaps, "source_status": [],
            "authority_policy": SOURCE_POLICY, "positioning_policy": POSITIONING,
            "vintage_note": "历史补采的官方定期原件以公布时间重建，当前抓取时间单列。日期级日报保守按下一自然日零点可用。历史字节从首次本机归档起可复验。行业、上下游与日程行只作对应口径参考。"}


def collect_agent_sources(products: list[str], *, archive_root: Path, cutoff: datetime | None = None) -> dict:
    """当前研究先取得原件再冻结上下文；指定历史时点时只查询档案。"""
    if cutoff is not None:
        return lookup_agent_sources(products, archive_root=archive_root, cutoff=cutoff)
    today = datetime.now(CN).date()
    statuses = collect_range(archive_root=archive_root, start=today-timedelta(days=4), end=today, live=True)
    result = lookup_agent_sources(products, archive_root=archive_root, cutoff=datetime.now(CN))
    result["source_status"] = statuses
    return result


def verify_archive(root: Path, start: date, end: date) -> dict:
    """逐份复算原件摘要并从相同字节复核每条入库事实。"""
    db_path = root / "facts.sqlite3"
    db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    by_blob = {}
    direct = set()
    scopes = Counter()
    try:
        for data, in db.execute("SELECT data FROM facts ORDER BY fact_id"):
            item = json.loads(data)
            identity = (item["url"], item["sha256"])
            by_blob.setdefault(identity, []).append(item)
            if item["scope"] == "direct":
                direct.update(item["products"])
            scopes[item["scope"]] += 1
    finally:
        db.close()
    manifest = []
    for (url, digest), items in sorted(by_blob.items()):
        raw = (root / "blobs" / digest).read_bytes()
        if sha256(raw).hexdigest() != digest:
            raise ValueError(f"原件摘要不符 {url}")
        meta = items[0]
        kind = meta["channel"]
        if kind == "calendar":
            parsed = _nbs(raw, start, True)
        else:
            m = re.search(r"(?:/t|dailystock_|/)(20\d{6})(?:_|/)", url)
            if not m:
                raise ValueError(f"官方原件地址缺少数据日期 {url}")
            day = datetime.strptime(m[1], "%Y%m%d").date()
            parser = {"shfe": _shfe, "czce": _czce, "nbs": _nbs, "moa": _moa}[kind]
            parsed = parser(raw, day)
        claims = {(product, f["title"], f["excerpt"], f["scope"], f["scheduled_at"], f["published_at"])
                  for f in parsed for product in f["products"]}
        for item in items:
            for product in item["products"]:
                if (product, item["title"], item["excerpt"], item["scope"], item["scheduled_at"], item["published_at"]) not in claims:
                    raise ValueError(f"入库字段与官方原件不符 {url}")
        manifest.append({"channel": kind, "url": url, "sha256": digest, "size": len(raw),
                         "published_at": meta["published_at"], "available_at": min(i["available_at"] for i in items),
                         "retrieved_at": meta["retrieved_at"], "fact_count": len(items)})
    return {"window": [start.isoformat(), end.isoformat()], "verified_at": _stamp(datetime.now(CN)),
            "archive_scope": "包含研究窗口前预热资料和事前事件日程，使用时按各决策时点截取。",
            "sqlite_sha256": sha256(db_path.read_bytes()).hexdigest(),
            "document_count": len(manifest), "facts_by_scope": dict(sorted(scopes.items())),
            "direct_products": sorted(direct), "sources": manifest}


def main():
    parser = argparse.ArgumentParser(description="采集或复核期策官方研究资料，日期按北京时间")
    parser.add_argument("--archive-root", type=Path, required=True, help="原件、索引和回执的私有目录")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--verify-only", action="store_true", help="离线从官方原件逐项核对全部归档事实")
    parser.add_argument("--official-urls", type=Path, help="补充官方发布地址清单，每行一条（限已支持的目录）")
    args = parser.parse_args()
    root = args.archive_root.resolve()
    if args.end < args.start:
        parser.error("结束日期早于开始日期")
    if not args.verify_only:
        statuses = []
        if args.official_urls:
            for url in args.official_urls.read_text(encoding="utf-8").splitlines():
                u = url.strip()
                if not u:
                    continue
                matched = re.fullmatch(r"https://(www\.stats\.gov\.cn/sj/zxfb|(?:scs|xmsyj)\.moa\.gov\.cn/jcyj)/\d{6}/t(\d{8})_\d+\.html?", u)
                if not matched:
                    parser.error("补充地址不属于已配置的官方原稿目录")
                day = datetime.strptime(matched[2], "%Y%m%d").date()
                if args.start <= day <= args.end:
                    statuses.append(_ingest(root, ("nbs" if u.startswith(NBS) else "moa", day, u), True))
        statuses += collect_range(archive_root=root, start=args.start, end=args.end)
        _write(root / "reports" / f"retrievals-{args.start}-{args.end}.json", json.dumps(statuses, ensure_ascii=False).encode())
    manifest = verify_archive(root, args.start, args.end)
    path = root / "reports" / f"manifest-{args.start}-{args.end}.json"
    _write(path, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    print(json.dumps({"document_count": manifest["document_count"], "fact_count": sum(manifest["facts_by_scope"].values()),
                      "direct_products": manifest["direct_products"], "manifest": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
