from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO

import pytest
from openpyxl import Workbook

from app import agent_sources as sources


def official_item(root, when, retrieved, text, *, title="场内现货", scheduled=None):
    raw = text.encode()
    digest = sha256(raw).hexdigest()
    sources._write(root / "blobs" / digest, raw)
    receipt = {"url": "https://www.stats.gov.cn/sj/zxfb/202606/t20260610_1000.html",
               "sha256": digest, "retrieved_at": sources._stamp(retrieved), "last_modified": None}
    fact = sources._fact(["M"], "fundamentals", "direct", title, text, when, scheduled=scheduled)
    sources._store(root, "nbs", receipt, [fact])
    return root / "blobs" / digest


def test_publisher_date_and_discovered_revision_cannot_leak_future_facts(tmp_path):
    utc = timezone.utc
    reported = datetime(2026, 6, 10, 7, tzinfo=utc)
    first_capture = datetime(2026, 10, 10, 7, tzinfo=utc)
    revised_at = datetime(2026, 11, 20, 7, tzinfo=utc)
    path = official_item(tmp_path, reported, first_capture, "原始批次 100")
    official_item(tmp_path, reported, revised_at, "修订为 85")
    official_item(tmp_path, reported, revised_at, "新增观察值", title="后来增加的栏目")
    lookup = lambda cutoff: sources.lookup_agent_sources(["M"], archive_root=tmp_path, cutoff=cutoff)["items"]

    assert lookup(datetime(2026, 6, 10, 6, 59, tzinfo=utc)) == []
    early = lookup(reported)
    assert [entry["excerpt"] for entry in early] == ["原始批次 100"]
    assert early[0]["retrieved_at"] == sources._stamp(first_capture)
    assert [entry["vintage"] for entry in early] == ["reconstructed_official_release"]
    assert {entry["excerpt"] for entry in lookup(revised_at)} == {"修订为 85", "新增观察值"}
    path.write_bytes(b"corrupt")
    assert lookup(reported) == []


def test_official_inventory_forecast_is_labeled_and_uses_next_day(tmp_path):
    workbook = Workbook()
    for row in (
        ["郑州商品交易所期货仓单日报表(2026-10-30)"],
        ["品种：苹果AP   单位：张   日期：2026-10-30"],
        ["机构编号", "机构简称", "预报数量", "当日增减"],
        ["总计", "", 10, -2],
        ["品种：甲醇MA   单位：张   日期：2026-10-30"],
        ["仓库编号", "仓库简称", "仓单数量(完税)", "仓单数量(保税)"],
        ["总计", "", 14, 6],
    ):
        workbook.active.append(row)
    raw = BytesIO()
    workbook.save(raw)
    facts = sources._czce(raw.getvalue(), date(2026, 10, 30))
    assert len(facts) == 3
    assert "预报数量总计 10 张" in facts[0]["excerpt"]
    assert facts[0]["published_at"] == "2026-10-30T16:00:00.000000+00:00"
    assert [row["title"] for row in facts[1:]] == ["MA 交易所仓单数量(完税)", "MA 交易所仓单数量(保税)"]
    with pytest.raises(ValueError, match="日期"):
        sources._czce(raw.getvalue(), date(2026, 10, 31))


def test_source_tier_hostname_and_port_are_exact_and_ownership_unknown():
    for address in ("https://www.stats.gov.cn.example.com/blog", "https://www.stats.gov.cn@fake.test/",
                    "https://www.stats.gov.cn:8888/", "http://www.stats.gov.cn/", "https://example.com/news"):
        with pytest.raises(ValueError, match="官方"):
            sources._issuer(address)
    assert sources._issuer("https://www.stats.gov.cn/sj/zxfb/") == "国家统计局"
    assert sources.POSITIONING["classification"] == "unknown"
    assert sources.POSITIONING["exchange_membership_is_proprietary_evidence"] is False


def test_retrieval_failure_still_returns_archived_sources(tmp_path, monkeypatch):
    day = datetime(2026, 6, 10, 7, tzinfo=timezone.utc)
    official_item(tmp_path, day, day, "冻结证据")
    monkeypatch.setattr(sources, "collect_range", lambda **_: pytest.fail("历史查询触发了外网请求"))
    packet = sources.collect_agent_sources(["M", "JD"], archive_root=tmp_path, cutoff=day)
    assert [f["products"] for f in packet["items"]] == [["M"]]
    assert any(g["dimension"] == "fundamentals" and g["product"] == "JD" for g in packet["gaps"])


def test_authoritative_calendar_contains_only_known_schedule_before_event():
    months = ["……" if i != 5 else "10/三" for i in range(12)]
    cells = "".join(f"<td>{m}</td>" if m != "……" else f"<td rowspan='2'>{m}</td>" for m in months)
    raw = f"""<meta name="ArticleTitle" content="2026年国家统计局主要统计信息发布日程表">
        <meta name="PubDate" content="2025/12/25 09:30"><div class="txt-content"><table>
        <tr><td rowspan="2">5</td><td rowspan="2">居民消费价格指数月度报告</td>{cells}</tr>
        <tr><td>9:30</td></tr></table></div>""".encode()
    events = sources._nbs(raw, date(2026, 1, 1), True)
    assert len(events) == 1
    assert events[0]["published_at"].startswith("2025-12-25")
    assert events[0]["scheduled_at"] == "2026-06-10T01:30:00.000000+00:00"
    assert events[0]["scope"] == "sector_calendar"
    assert events[0]["dimension"] == "news"
