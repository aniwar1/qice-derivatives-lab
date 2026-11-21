import json

import pytest

from scripts.collect_replay_market import parse, revision_metadata


def test_shfe_actual_vintage_and_report_year():
    raw = json.dumps(
        {"report_date": "20260807", "update_date": "20260808 11:29:54"}
    ).encode()
    assert (
        revision_metadata("SHFE", "2026-08-07", raw)["disclosed_available_at"]
        == "2026-08-08T11:29:54+08:00"
    )
    with pytest.raises(ValueError):
        revision_metadata("SHFE", "2025-08-07", raw)


def test_standard_czce_options_keep_zero_volume_and_published_iv_units():
    raw = (
        "郑州商品交易所期权 2026-08-07\n"
        "合约代码|今开盘|最高价|最低价|今收盘|成交量(手)|持仓量|隐含波动率\n"
        "SM609C7000|18.50|19|18|19|20|55|37.85\n"
        "SM609P6000|0|0|0|0|0|2|\n"
    ).encode()
    rows, counts = parse("CZCE", "option", "2026-08-07", raw, {"SM": "CZCE"})
    assert counts == {"SM": 2}
    assert rows["SM2609C7000"] == [18.5, 19, 18, 19, 20, 55, 37.85]
    assert rows["SM2609P6000"] == [0, 0, 0, 0, 0, 2, None]


def test_monthly_revised_attachment_must_wait_for_latest_official_version():
    from datetime import date
    from scripts.collect_monthly_statistics import available_at

    assert (
        available_at(date(2026, 9, 10), "Wed, 16 Sep 2026 01:24:49 GMT").isoformat()
        == "2026-09-16T09:24:49+08:00"
    )
    assert available_at(date(2026, 9, 10)).isoformat() == "2026-09-11T00:00:00+08:00"
