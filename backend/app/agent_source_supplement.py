"""根据锁定的原始披露补齐三个品种的经营数据，独立保存本次历史资料。"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import requests

from .agent_sources import CN, _raw_verified, _stamp, _write


CNINFO = "https://static.cninfo.com.cn/finalpage/"
NBS = "https://www.stats.gov.cn/sj/sjjd/"
MIIT = "https://wap.miit.gov.cn/gxsj/tjfx/dzxx/art/2026/art_09763880ab3b4f3da2dcd747b0a4058d.html"
SILICON = "合盛硅业股份有限公司"
PVC = "上海氯碱化工股份有限公司"
LITHIUM = "青海盐湖工业股份有限公司"


@dataclass(frozen=True)
class Release:
    url: str
    product: str
    publisher: str
    period: str
    method: str
    page: int = 1


RELEASES = (
    Release(CNINFO + "2026-04-29/1225223453.PDF", "V", PVC, "2026年第一季度", "pvc"),
    Release(CNINFO + "2026-08-25/1225499086.PDF", "V", PVC, "2026年半年度", "pvc"),
    Release(CNINFO + "2026-10-28/1225628222.PDF", "V", PVC, "2026年前三季度", "pvc"),
    Release(CNINFO + "2026-04-30/1225255725.PDF", "SI", SILICON, "2026年第一季度", "silicon"),
    Release(CNINFO + "2026-08-26/1225505214.PDF", "SI", SILICON, "2026年第二季度", "silicon"),
    Release(CNINFO + "2026-08-26/1225505925.PDF", "SI", SILICON, "2026年半年度", "management", 7),
    Release(CNINFO + "2026-10-30/1225644105.PDF", "SI", SILICON, "2026年第三季度", "silicon"),
    Release(CNINFO + "2026-04-30/1225260305.PDF", "LC", LITHIUM, "2026年第一季度", "salt_q1", 6),
    Release(CNINFO + "2026-08-26/1225504164.PDF", "LC", LITHIUM, "2026年半年度", "salt_h1", 19),
    Release(CNINFO + "2026-10-30/1225642395.PDF", "LC", LITHIUM, "2026年前三季度", "salt_ytd", 6),
    Release(NBS + "202606/t20260616_1963959.html", "LC", "国家统计局", "2026年5月", "nbs_may"),
    Release(NBS + "202609/t20260915_1965330.html", "LC", "国家统计局", "2026年8月", "nbs_aug"),
    Release(MIIT, "LC", "工业和信息化部电子信息司", "2026年上半年", "miit"),
)
BY_URL = {report.url: report for report in RELEASES}


def _extract_pdf(report, raw):
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(raw))
    body = reader.pages[report.page - 1].extract_text(extraction_mode="layout")
    compact = re.sub(r"\s+", "", body)
    first = re.sub(r"\s+", "", reader.pages[0].extract_text())
    last = re.sub(r"\s+", "", reader.pages[-1].extract_text())
    day = date.fromisoformat(urlsplit(report.url).path.split("/")[-2])
    signature = f"{day.year}年{day.month}月{day.day}日"
    report_title = "2026年第三季度" if report.method == "salt_ytd" else report.period
    if report.method == "salt_h1":
        # 该期封面是图像，发行日期另由巨潮公告索引印证。
        first = re.sub(r"\s+", "", reader.pages[3].extract_text())
        identity_ok = report.publisher in first and "半年度报告" in first
    else:
        identity_ok = report.publisher in first and report_title in first and signature in last
    if not identity_ok:
        raise ValueError("发行人、报告期或署名日期未通过原件核对")
    if report.method in {"pvc", "silicon"}:
        name, unit = ("聚氯乙烯", "万吨") if report.method == "pvc" else ("工业硅", "吨")
        if unit not in body or not re.search("产量|生产量", compact):
            raise ValueError("产销表头或计量单位缺失")
        table = body.split("二、")[0]
        row = re.search(rf"^\s*{name}(?:产品)?\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)", table, re.M)
        if not row:
            raise ValueError("产销行与指定品种不符")
        metrics = {"production": row[1], "sales": row[2], "unit": unit}
        detail = f"{name}产量 {row[1]} {unit}，销量 {row[2]} {unit}"
    elif report.method == "management":
        if "工业硅产能利用率同比提升，产销量实现显著增长" not in compact:
            raise ValueError("管理层说明缺少工业硅同比比较")
        metrics = {}
        detail = "管理层披露公司工业硅产能利用率同比提高、产销量增长（定性说明）"
    elif report.method == "salt_h1":
        match = re.search(r"碳酸锂产量([\d.]+)万吨、销量([\d.]+)万吨", compact)
        if not match:
            raise ValueError("半年报缺少碳酸锂产销量")
        metrics = {"production": match[1], "sales": match[2], "unit": "万吨"}
        detail = f"碳酸锂产量 {match[1]} 万吨，销量 {match[2]} 万吨，含五矿盐湖并表"
    elif report.method == "salt_q1":
        match = re.search(r"碳酸锂产量约([\d.]+)万吨，销量约([\d.]+)万吨", compact)
        if not match:
            raise ValueError("原件缺少碳酸锂产销量")
        metrics = {"production_approx": match[1], "sales_approx": match[2], "unit": "万吨"}
        detail = f"碳酸锂产量约 {match[1]} 万吨，销量约 {match[2]} 万吨"
    else:
        match = re.search(r"碳酸锂产量([\d.]+)万吨，同比增加([\d.]+)%", compact)
        if not match:
            raise ValueError("原件缺少碳酸锂产量与同比")
        metrics = {"production": match[1], "unit": "万吨", "production_yoy_pct": match[2]}
        detail = f"前三季度累计碳酸锂产量 {match[1]} 万吨，同比增长 {match[2]}%"
    title = f"{report.publisher} · {report.period}经营披露"
    return title, f"{report.publisher}{report.period}：{detail}。仅限本企业，用作低频背景。", metrics, day


def _extract_html(report, raw):
    soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser")
    content = re.sub(r"\s+", "", soup.get_text())
    if report.method == "miit":
        title = "2026年上半年全国锂离子电池行业运行情况"
        publication = re.search(r"发布时间[：:]\s*(\d{4}-\d\d-\d\d\s+\d\d:\d\d)", soup.get_text(" ", strip=True))
        match = re.search(r"电池级碳酸锂产量([\d.]+)万吨，同比增长([\d.]+)%", content)
        if title not in content or not publication or not match or "电子信息司" not in content:
            raise ValueError("部委原稿数据、统计说明或来源缺失")
        at = datetime.fromisoformat(publication[1]).replace(tzinfo=CN)
        metrics = {"production": match[1], "unit": "万吨", "production_yoy_pct": match[2]}
        detail = f"上半年全国电池级碳酸锂产量 {match[1]} 万吨，同比增长 {match[2]}%。主管部门依据规范公告企业信息和行业协会测算。"
    else:
        title = soup.select_one('meta[name="ArticleTitle"]')["content"]
        at = datetime.strptime(soup.select_one('meta[name="PubDate"]')["content"], "%Y/%m/%d %H:%M").replace(tzinfo=CN)
        if report.period + "份工业生产数据" not in title:
            raise ValueError("统计局原稿的标题与报告期不符")
        pattern = (r"碳酸锂、燃料乙醇、生物基化学纤维等产品产量分别增长([\d.]+)%" if report.method == "nbs_may"
                   else r"碳纤维及其复合材料、碳酸锂等产品产量分别增长[\d.]+%、([\d.]+)%")
        match = re.search(pattern, content)
        if not match:
            raise ValueError("统计局列举的产品顺序或指标已变化")
        metrics = {"production_yoy_pct": match[1]}
        detail = f"{report.period}全国规模以上工业企业碳酸锂产量同比增长 {match[1]}%。"
    return title, detail, metrics, at


def extract(report, raw, observed, last_modified):
    is_pdf = report.url.endswith(".PDF")
    if is_pdf:
        title, excerpt, metrics, day = _extract_pdf(report, raw)
        # 巨潮归档日期与公司签署日一致，仅能确认日期，次日零时方可使用。
        at = datetime.combine(day + timedelta(days=1), time.min, CN)
        channel, scope = "issuer_disclosure", "issuer_operations"
    else:
        title, excerpt, metrics, at = _extract_html(report, raw)
        day = at.date()
        channel, scope = ("nbs_commentary" if report.publisher == "国家统计局" else "miit_industry"), "national_product_statistics"
    modified = parsedate_to_datetime(last_modified) if last_modified else None
    available = max(at, modified) if modified else at
    digest = sha256(raw).hexdigest()
    return {"id": sha256((report.url + digest).encode()).hexdigest(), "products": [report.product],
            "title": title, "excerpt": excerpt, "metrics": metrics, "period": report.period, "scope": "direct",
            "authority": "primary_official", "coverage_scope": scope, "dimension": "fundamentals",
            "publisher": report.publisher, "channel": channel, "url": report.url, "page": report.page if is_pdf else None,
            "sha256": digest, "raw": f"originals/{digest}." + ("pdf" if is_pdf else "html"),
            "published_day": day.isoformat(), "published_at": _stamp(at), "available_at": _stamp(available),
            "retrieved_at": observed, "last_modified": last_modified, "time_precision": "day" if is_pdf else "minute",
            "freshness_role": "low_frequency_background_only" if is_pdf else "dated_national_statistics",
            "ownership": "unknown", "vintage": "reconstructed_official_release", "scheduled_at": None}


def _filing_receipt(root, report, old):
    if report.method != "salt_h1":
        return {}
    url = "https://www.cninfo.com.cn/new/announcement/bulletin_detail"
    if old:
        raw = (root / old["disclosure_index_raw"]).read_bytes()
    else:
        response = requests.post(url, data={"announceId": "1225504164", "announcementTime": "2026-08-26"},
                                 timeout=(5, 12), allow_redirects=False)
        response.raise_for_status()
        raw = response.content
    record = json.loads(raw)["announcement"]
    announced = datetime.fromtimestamp(record["announcementTime"] / 1000, CN).date().isoformat()
    if (record["secCode"] != "000792" or announced != "2026-08-26" or record["announcementTitle"] != "2026年半年度报告"
            or "https://static.cninfo.com.cn/" + record["adjunctUrl"] != report.url):
        raise ValueError("巨潮披露记录的发行人、日期、报告名称或原件地址不符")
    digest = sha256(raw).hexdigest()
    name = f"originals/{digest}.json"
    if not old:
        _write(root / name, raw)
    return {"disclosure_index_raw": name, "disclosure_index_sha256": digest, "disclosure_index_url": url}


def collect(root, *, verify_only=False):
    index = root / "index.json"
    prior = json.loads(index.read_bytes()) if index.is_file() else {"items": [], "gaps": []}
    by_url = {item["url"]: item for item in prior["items"]}
    results, gaps = [], []
    for report in RELEASES:
        old = by_url.get(report.url)
        if old:
            raw = (root / old["raw"]).read_bytes()
            checked = extract(report, raw, old["retrieved_at"], old["last_modified"])
            checked.update(_filing_receipt(root, report, old))
            if old != checked:
                raise ValueError("资料索引与原件解析结果不一致")
            results.append(checked)
            continue
        if verify_only:
            if not any(g["url"] == report.url for g in prior["gaps"]):
                raise ValueError("索引中缺少指定资料及缺口说明")
            continue
        try:
            response = requests.get(report.url, timeout=(5, 12), allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200 or response.url != report.url:
                raise ValueError("未取得指定发布者的完整原件")
            raw = response.content
            checked = extract(report, raw, _stamp(datetime.now(timezone.utc)), response.headers.get("Last-Modified"))
            checked.update(_filing_receipt(root, report, None))
        except (requests.RequestException, ValueError) as exc:
            gaps.append({"url": report.url, "product": report.product, "reason": str(exc)[:90]})
            continue
        _write(root / checked["raw"], raw)
        results.append(checked)
    if verify_only:
        if len(results) != len(prior["items"]):
            raise ValueError("索引中存在清单外资料")
        return prior
    value = {"schema_version": 1, "items": results, "gaps": gaps}
    _write(index, json.dumps(value, ensure_ascii=False, indent=2).encode())
    return value


@lru_cache(maxsize=4)
def _read_index(path, mtime_ns, size):
    return json.loads(path.read_bytes())["items"]


def visible_items(root, cutoff):
    path = root / "supplement" / "index.json"
    if not path.is_file():
        return {}
    stat = path.stat()
    items = _read_index(path, stat.st_mtime_ns, stat.st_size)
    latest = {}
    for item in items:
        report = BY_URL.get(item["url"])
        if not report or item["products"] != [report.product] or item["publisher"] != report.publisher:
            continue
        at = datetime.fromisoformat(item["available_at"])
        corporate = report.url.endswith(".PDF")
        if not timedelta() <= cutoff - at <= timedelta(days=125 if corporate else 45):
            continue
        key = (report.product, report.publisher)
        # 同日有半年报定性说明和季度产销量公告时选有数据的公告。
        rank = (at, bool(item["metrics"]))
        if key not in latest or latest[key][0] < rank:
            latest[key] = (rank, item)
    grouped = {}
    for (symbol, _), (_, item) in latest.items():
        # 新版本一经披露便替代同主体旧报告，原件损坏只形成缺口。
        digest = item["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        suffix = ".pdf" if item["url"].endswith(".PDF") else ".html"
        required = [(digest, suffix)]
        if BY_URL[item["url"]].method == "salt_h1":
            required.append((item.get("disclosure_index_sha256", ""), ".json"))
        for sha, ext in required:
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                break
            raw = path.parent / "originals" / (sha + ext)
            if not raw.is_file():
                break
            stat = raw.stat()
            if not _raw_verified(raw, sha, stat.st_size, stat.st_mtime_ns):
                break
        else:
            grouped.setdefault(symbol, []).append(item)
    return grouped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    result = collect(args.out, verify_only=args.verify_only)
    print(json.dumps({"items": len(result["items"]), "gaps": result["gaps"]}, ensure_ascii=False))
