import gzip
from datetime import datetime
from hashlib import sha256

import pytest
from app import rolling_marks
from app import rolling_update as roll
from app.agent_replay_account import Account
from app.study_view import BATCH

DAYS = ("2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25")
LEG = "FG2701P890"


def stamp(day, clock):
    return datetime.fromisoformat(day + "T" + clock + ":00+08:00")


def seed(root, pos=None):
    groups = {}
    for group in "ABC":
        account = Account(group)
        if group == "A" and pos:
            account.cash -= pos["cost"]
            account.opened = 1
            account.positions = [pos]
            account.previous_equity = account.cash + pos["value"]
        groups[group] = {
            "variant": "baseline",
            "account": vars(account),
            "point": {"day": DAYS[0], "equity": account.previous_equity},
        }
    return roll._seal(
        roll._folder(root) / "seed.json",
        {
            "day": DAYS[0],
            "recorded_at": stamp(DAYS[0], "16:10").isoformat(),
            "baseline_end": DAYS[0],
            "groups": groups,
        },
    )


def position(end="2026-12-11"):
    return {
        "age": 2,
        "cost": 200.0,
        "day": "2026-11-17",
        "exit_reason": "达到预设期限",
        "hold": 4,
        "invalid": 1000,
        "legs": [LEG],
        "mult": 20,
        "product": "FG",
        "qty": 1,
        "side": "P",
        "target_exit": None,
        "underlying": "FG2701",
        "value": 180.0,
        "expiry_legs": {
            LEG: {"expiry_at": end + "T15:00:00+08:00", "expiry_sha256": "a" * 64}
        },
    }


def fixture_day(root, day, *, traded=0, mark=12, interest=5, future=930):
    market = roll._folder(root) / "market"
    market.mkdir(parents=True, exist_ok=True)
    sources = []
    for kind, code, value in (
        ("future", "FG701", future),
        ("option", "FG701P890", mark),
    ):
        if value is None:
            continue
        raw = (
            day
            + ("期货" if kind == "future" else "期权")
            + "交易日报\n合约代码|今结算|持仓量|行权量\n"
            + code
            + "|"
            + str(value)
            + "|"
            + str(interest)
            + "|0\n"
        ).encode()
        path = market / (day + "-" + kind + ".gz")
        path.write_bytes(gzip.compress(raw))
        sources.append(
            {
                "day": day,
                "type": kind,
                "exchange": "CZCE",
                "sha256": sha256(raw).hexdigest(),
                "raw": path.name,
                "retrieved_at": stamp(day, "16:01").isoformat(),
            }
        )
    px = {
        "future": {"FG2701": [future, 940, 921, future, 50, 1000, None]},
        "option": {LEG: [10, 13, 8, 11, traded, 5, None]},
    }
    return px, sources, []


def wait():
    return {
        "product": "",
        "action": "wait",
        "news": "缺少新消息",
        "technical": "技术依据有限",
        "fundamentals": "缺最新数据",
        "ownership": "席位无法认定自营",
        "thesis": "继续观望",
        "invalidation": "等待新证据",
        "evidence_ids": [],
        "legs": [],
        "risk_bps": 0,
        "holding_days": 0,
        "invalidation_price": 0,
        "scenario_target": 0,
        "take_profit_close": 0,
        "max_lots": 0,
        "max_entry_premium": 0,
        "scenario_low": 0,
        "scenario_high": 0,
    }


def mock_time(monkeypatch, day, clock):
    monkeypatch.setattr(roll, "_now", lambda: stamp(day, clock))


def test_weekend_and_missing_preopen_do_not_manufacture_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(roll, "trading_days", lambda root: DAYS)
    prior = seed(tmp_path, position())
    mock_time(monkeypatch, "2026-11-22", "12:00")
    assert roll.tick(tmp_path)["status"] == "waiting"
    assert not (roll._folder(tmp_path) / "decisions").exists()
    mock_time(monkeypatch, DAYS[1], "16:20")
    monkeypatch.setattr(roll, "_market", lambda root, day: fixture_day(root, day))
    settled = roll.tick(tmp_path)
    assert settled["decision_status"] == "missed_preopen"
    first = roll.latest_account_snapshot(tmp_path)
    assert first["prev_sha256"] == prior["sha256"]
    group = first["groups"]["A"]
    assert (
        group["decision"] is None and group["execution"]["status"] == "no_sealed_signal"
    )
    assert not group["execution"]["exits"]
    assert group["point"]["equity"] == 200040
    assert group["point"]["official_settlement_marks"][0]["leg"] == LEG
    assert group["account"]["positions"][0]["value"] == 240
    mock_time(monkeypatch, DAYS[2], "16:20")
    monkeypatch.setattr(
        roll, "_market", lambda root, day: fixture_day(root, day, traded=20)
    )
    assert roll.tick(tmp_path)["status"] == "settled"
    following = roll.latest_account_snapshot(tmp_path)
    assert following["prev_sha256"] == first["sha256"]
    assert following["groups"]["A"]["execution"]["exits"][0]["raw_open_prices"] == [10]
    assert following["groups"]["A"]["point"]["open_positions"] == 0
    with pytest.raises(ValueError, match="不能覆盖"):
        roll._seal(
            roll._folder(tmp_path) / "settlements" / (DAYS[1] + ".json"),
            {"changed": True},
        )


def test_missing_exchange_or_closing_number_cannot_publish_flat(tmp_path, monkeypatch):
    prior = seed(tmp_path, position())
    monkeypatch.setattr(
        roll, "_market", lambda root, day: fixture_day(root, day, mark=None)
    )
    with pytest.raises(roll.DataGap, match="交易所"):
        roll._settle(tmp_path, prior, DAYS[1])
    monkeypatch.setattr(
        roll, "_market", lambda root, day: fixture_day(root, day, mark="--")
    )
    with pytest.raises(roll.DataGap, match="数值"):
        roll._settle(tmp_path, prior, DAYS[1])
    assert not (roll._folder(tmp_path) / "settlements").exists()


@pytest.mark.parametrize(("future", "expected"), [(930, "settled"), (880, "pending")])
def test_expiry_requires_official_terminal_zero_and_otm(
    tmp_path, monkeypatch, future, expected
):
    monkeypatch.setattr(roll, "trading_days", lambda root: DAYS)
    rule = tmp_path / "local-data/agent-study" / (BATCH + "-frequency") / "expiry-rules"
    rule.mkdir(parents=True)
    data = "测试规则".encode()
    fingerprint = sha256(data).hexdigest()
    (rule / (fingerprint + ".pdf")).write_bytes(data)
    monkeypatch.setattr(rolling_marks, "ABANDON_HASH", fingerprint)
    seed(tmp_path, position(end=DAYS[1]))
    mock_time(monkeypatch, DAYS[1], "16:20")
    monkeypatch.setattr(
        roll,
        "_market",
        lambda root, day: fixture_day(root, day, mark=0, interest=0, future=future),
    )
    result = roll.tick(tmp_path)
    assert result["status"] == expected
    account = roll.latest_account_snapshot(tmp_path)["groups"]["A"]
    if expected == "settled":
        assert account["account"]["realized"] == -200
        assert (
            account["execution"]["exits"][0]["expiry_evidence"]["rule_sha256"]
            == fingerprint
        )
        assert account["point"]["open_positions"] == 0
    else:
        assert account["point"]["day"] == DAYS[0]
        assert "转仓" in result["reason"]


def test_prewindow_freeze_then_one_model_receipt_and_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(roll, "trading_days", lambda root: DAYS)
    prior = seed(tmp_path)
    packet = {
        "trade_date": DAYS[1],
        "previous_account_sha256": prior["sha256"],
        "products": {},
        "documents": [],
        "market_sources": {},
        "next_sessions": list(DAYS[1:]),
        "news_review_guidance": "已封存的研究指导",
    }
    calls = []
    monkeypatch.setattr(roll, "_packet", lambda *args: dict(packet))

    def decide(doc, model, cli, timeout):
        calls.append(doc)
        return {g: wait() for g in "ABC"}, {
            "run_id": "one",
            "model": model,
            "usage": {"input_tokens": 33},
            "prompt_sha256": "b" * 64,
        }

    monkeypatch.setattr(roll, "decide", decide)
    mock_time(monkeypatch, DAYS[0], "19:30")
    assert roll.tick(tmp_path)["status"] == "evidence_ready"
    mock_time(monkeypatch, DAYS[0], "20:05")
    assert roll.tick(tmp_path, cli="test")["status"] == "sealed_preopen"
    receipt = roll._read(roll._folder(tmp_path) / "decisions" / (DAYS[1] + ".json"))
    assert receipt["prev_sha256"] == prior["sha256"] and receipt[
        "context_sha256"
    ] == roll.digest(packet)
    assert len(calls) == 1 and calls[0]["news_review_guidance"] == "已封存的研究指导"
    assert roll.tick(tmp_path)["status"] == "awaiting_close"
    assert len(calls) == 1


def test_no_after_cutoff_input_or_late_model_return(tmp_path, monkeypatch):
    monkeypatch.setattr(roll, "trading_days", lambda root: DAYS)
    prior = seed(tmp_path)
    mock_time(monkeypatch, DAYS[0], "20:01")
    assert "20点以前" in roll.tick(tmp_path)["reason"]
    packet = {"trade_date": DAYS[1], "previous_account_sha256": prior["sha256"]}

    def late_packet(*args):
        mock_time(monkeypatch, DAYS[0], "20:01")
        return packet

    mock_time(monkeypatch, DAYS[0], "19:59")
    monkeypatch.setattr(roll, "_packet", late_packet)
    assert "跨过20点" in roll.tick(tmp_path)["reason"]
    mock_time(monkeypatch, DAYS[0], "19:50")
    monkeypatch.setattr(roll, "_packet", lambda *args: packet)
    assert roll.tick(tmp_path)["status"] == "evidence_ready"

    def late_model(*args, **kwargs):
        mock_time(monkeypatch, DAYS[0], "21:01")
        return {}, {}

    monkeypatch.setattr(roll, "decide", late_model)
    mock_time(monkeypatch, DAYS[0], "20:58")
    assert roll.tick(tmp_path, cli="test")["status"] == "pending"
    assert not (roll._folder(tmp_path) / "decisions").exists()


def test_post_cutoff_source_cannot_be_used_for_a_forward_packet(tmp_path, monkeypatch):
    import json

    from app import live_expiry

    root = tmp_path
    (root / "config").mkdir()
    (root / "config/product_contract_specs.json").write_text(
        json.dumps({"products": [{"symbol": "FG", "multiplier": 20}]})
    )
    prior = seed(root)
    monkeypatch.setattr(roll, "PRODUCT_EXCHANGES", {"FG": "CZCE"})
    monkeypatch.setattr(roll, "monthly_context", lambda *args: ([], {}))
    mock_time(monkeypatch, DAYS[0], "19:40")
    prices, records, _ = fixture_day(root, DAYS[0], traded=1000)
    base = {
        k: r
        | {"https": "https://www.czce.com.cn/", "configured_product_rows": {"FG": 1}}
        for r in records
        for k in [(DAYS[0], "CZCE", r["type"])]
    }

    def make_index(source_clock):
        return {
            k: r | {"retrieved_at": stamp(DAYS[0], source_clock).isoformat()}
            for k, r in base.items()
        }

    monkeypatch.setattr(
        roll, "_history", lambda *args: ({DAYS[0]: prices}, make_index("20:02"))
    )
    monkeypatch.setattr(
        roll,
        "_news",
        lambda *args: {
            "items": [],
            "source_status": [],
            "gaps": [],
            "authority_policy": {},
            "vintage_note": "待核",
            "positioning_policy": {},
        },
    )
    with pytest.raises(roll.DataGap, match="缺少可证实"):
        roll._packet(root, prior, DAYS[1], DAYS)
    monkeypatch.setattr(
        roll, "_history", lambda *args: ({DAYS[0]: prices}, make_index("16:02"))
    )

    def too_late(books, *, calendar):
        for book in books:
            book.update(expiry_at="2026-12-11T15:00:00+08:00", expiry_sha256="a" * 64)
        return [
            {"sha256": "a" * 64, "retrieved_at": stamp(DAYS[0], "20:01").isoformat()}
        ], {}

    monkeypatch.setattr(live_expiry, "collect_expiries", too_late)
    frozen = roll._packet(root, prior, DAYS[1], DAYS)
    assert len(frozen["products"]) == 1 and frozen["products"]["FG"]["technical"]
    assert not frozen["expiry_sources"] and not frozen["products"]["FG"]["options"]
    assert (
        frozen["paper_accounts"]["A"]["cash"] == 200000
        and frozen["news_review_guidance"]
    )


def test_observed_after_cutoff_documents_are_excluded_even_if_previously_dated(
    tmp_path, monkeypatch
):
    cutoff = stamp(DAYS[0], "20:00")
    payload = b"original"
    checksum = sha256(payload).hexdigest()
    folder = tmp_path / "local-data/agent-sources/blobs"
    folder.mkdir(parents=True)
    (folder / checksum).write_bytes(payload)
    valid = {
        "id": "early",
        "sha256": checksum,
        "published_at": stamp(DAYS[0], "16:00").isoformat(),
        "available_at": stamp(DAYS[0], "17:00").isoformat(),
        "retrieved_at": stamp(DAYS[0], "18:00").isoformat(),
    }
    late = valid | {"id": "later", "retrieved_at": stamp(DAYS[0], "20:01").isoformat()}
    revised = valid | {
        "id": "revision",
        "available_at": stamp(DAYS[0], "20:05").isoformat(),
    }
    monkeypatch.setattr(
        roll, "collect_agent_sources", lambda *args, **kwargs: {"source_status": []}
    )
    monkeypatch.setattr(
        roll,
        "lookup_agent_sources",
        lambda *args, **kwargs: {"items": [valid, late, revised]},
    )
    mock_time(monkeypatch, DAYS[0], "20:09")
    result = roll._news(tmp_path, cutoff)
    assert [i["id"] for i in result["items"]] == ["early"]
    assert (roll._folder(tmp_path) / "evidence" / checksum).read_bytes() == payload


def test_frozen_draft_protects_preexisting_seed(tmp_path):
    p = position()
    del p["expiry_legs"]
    old = seed(tmp_path, p)
    roll._seal(
        roll._folder(tmp_path) / "staged" / DAYS[1] / "pre.json",
        {"prev_sha256": old["sha256"]},
    )
    assert roll._upgrade_seed(tmp_path, old) == old
    assert roll._read(roll._folder(tmp_path) / "seed.json")["sha256"] == old["sha256"]


def test_monthly_late_workbook_keeps_prior_authoritative_issue(tmp_path, monkeypatch):
    from app import rolling_monthly as monthly

    old_dir = tmp_path / "local-data/agent-study" / BATCH / "monthly"
    (old_dir / "originals").mkdir(parents=True)

    def store_issue(folder, period, seen):
        html = b"original page" + period.encode()
        workbook = b"workbook" + period.encode()
        page = sha256(html).hexdigest()
        data = sha256(workbook).hexdigest()
        (folder / "originals").mkdir(parents=True, exist_ok=True)
        (folder / "originals" / (page + ".html")).write_bytes(html)
        (folder / "originals" / (data + ".xlsx")).write_bytes(workbook)
        return [
            {
                "product": "FG",
                "kind": "option",
                "month": period,
                "retrieved_at": stamp(DAYS[0], seen).isoformat(),
                "available_at": stamp(DAYS[0], "09:00").isoformat(),
                "page_sha256": page,
                "sha256": data,
            }
        ]

    old = store_issue(old_dir, "2026-09", "16:05")
    monthly.atomic(old_dir / "index.json", old)
    monthly.atomic(
        old_dir.parent / "run-manifest.json", {"monthly_sha256": monthly.digest(old)}
    )
    calls = []

    def fetch(out, *args):
        calls.append(out)
        records = store_issue(out, "2026-10", "20:01")
        monthly.atomic(out / "index.json", records)

    monkeypatch.setattr(monthly, "collect", fetch)
    limit = stamp(DAYS[0], "20:00")
    items, status = monthly.monthly_context(
        tmp_path, limit, lambda: limit, roll._store_blob
    )
    assert status["state"] == "observed" and items == old
    assert (roll._folder(tmp_path) / "evidence" / old[0]["sha256"]).exists()
    assert (
        monthly.monthly_context(tmp_path, limit, lambda: limit, roll._store_blob)[0]
        == old
        and len(calls) == 1
    )


def test_explicit_zero_settlement_keeps_unexpired_position_open(tmp_path, monkeypatch):
    prior = seed(tmp_path, position())
    monkeypatch.setattr(
        roll, "_market", lambda root, day: fixture_day(root, day, mark=0)
    )
    mock_time(monkeypatch, DAYS[1], "16:20")
    settled = roll._settle(tmp_path, prior, DAYS[1])
    g = settled["groups"]["A"]
    assert g["point"]["open_positions"] == 1
    assert g["account"]["positions"][0]["value"] == 0
    assert g["point"]["realized"] == 0 and g["execution"]["exits"] == []
    assert settled["latest_market_observed_at"] == stamp(DAYS[1], "16:01").isoformat()
