from datetime import datetime, timedelta
from hashlib import sha256
import json

from app import agent_source_supplement as supplement
from app.agent_sources import CN, lookup_agent_sources


def test_nbs_uses_percent_of_the_named_product_and_the_later_timestamp():
    release = next(r for r in supplement.RELEASES if r.method == "nbs_aug")
    raw = """<meta name="ArticleTitle" content="国家统计局解读2026年8月份工业生产数据">
    <meta name="PubDate" content="2026/09/15 15:00">
    <p>碳纤维及其复合材料、碳酸锂等产品产量分别增长19.1%、18.9%。</p>""".encode()
    item = supplement.extract(release, raw, "2026-11-21T14:00:00+00:00", "Wed, 16 Sep 2026 01:00:00 GMT")
    assert item["metrics"] == {"production_yoy_pct": "18.9"}
    assert item["available_at"] == "2026-09-16T01:00:00.000000+00:00"
    assert item["published_at"] == "2026-09-15T07:00:00.000000+00:00"


def test_company_coverage_is_point_in_time_superseded_and_bounded(tmp_path):
    root = tmp_path / "supplement"
    (root / "originals").mkdir(parents=True)
    start = datetime(2026, 5, 1, tzinfo=CN)
    later = start + timedelta(days=118)
    rows = []
    for method, stamp in (("salt_q1", start), ("salt_h1", later)):
        release = next(r for r in supplement.RELEASES if r.method == method)
        data = method.encode()
        digest = sha256(data).hexdigest()
        (root / "originals" / f"{digest}.pdf").write_bytes(data)
        digest_extra = sha256(b"index").hexdigest()
        (root / "originals" / f"{digest_extra}.json").write_bytes(b"index")
        rows.append({"disclosure_index_sha256": digest_extra, "url": release.url, "products": ["LC"], "publisher": release.publisher,
                     "available_at": stamp.isoformat(), "sha256": digest, "metrics": {"production": 10},
                     "dimension": "fundamentals", "scope": "direct", "period": release.period})
    (root / "index.json").write_text(json.dumps({"items": rows}))
    def periods(when):
        return [i["period"] for i in lookup_agent_sources(["LC"], archive_root=tmp_path, cutoff=when)["items"]]
    assert periods(start - timedelta(microseconds=1)) == []
    assert periods(start + timedelta(days=110)) == ["2026年第一季度"]
    assert periods(later) == ["2026年半年度"]
    assert periods(later + timedelta(days=125, seconds=1)) == []
    (root / "originals" / f"{rows[1]['sha256']}.pdf").write_bytes(b"damaged")
    assert periods(later + timedelta(days=1)) == []
