from app.market_psychology import attach_reaction_history


def test_market_reaction_uses_visible_versions_and_one_actual_contract():
    days = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
    packet = {
        "cutoff": days[-1] + "T20:00:00+08:00",
        "products": {"FG": {"exchange": "CZCE", "technical": {"contract": "FG2701"}}},
        "market_sources": {},
    }
    market = {
        d: {"future": {"FG2701": [0, 0, 0, 950, i, 700]}} for i, d in enumerate(days)
    }
    sources = {
        (d, "CZCE", "future"): {
            "day": d,
            "exchange": "CZCE",
            "type": "future",
            "sha256": f"{i:064x}",
            "https": "https://www.czce.com.cn/",
            "retrieved_at": d + "T17:00:00+08:00",
            "configured_product_rows": {"FG": 1},
        }
        for i, d in enumerate(days)
    }
    market[days[0]]["future"] = {"FG2702": [0, 0, 0, 900, 125, 800]}
    del sources[(days[1], "CZCE", "future")]
    sources[(days[2], "CZCE", "future")]["disclosed_available_at"] = (
        "2026-11-21T01:00:00+08:00"
    )
    sources[(days[4], "CZCE", "future")]["retrieved_at"] = "2026-11-20T20:01:00+08:00"
    attach_reaction_history(packet, days, market, sources)
    rows = packet["products"]["FG"]["reaction_history"]
    assert rows == [
        {
            "day": days[3],
            "contract": "FG2701",
            "close": 950,
            "volume": 3,
            "open_interest": 700,
            "source_id": "CZCE-future-2026-11-19",
        }
    ]
    assert packet["market_sources"][rows[0]["source_id"]]["sha256"] == f"{3:064x}"


def test_live_analysis_keeps_only_completed_daily_reactions_before_cutoff():
    from datetime import datetime
    from app.market_psychology import live_observation_context
    from app.research_archive import MarketObservation

    at = datetime.fromisoformat("2026-11-20T10:10:00+08:00")
    bar = {"close": 950, "volume": 112, "hold": 700, "completed": True}
    published = MarketObservation(
        product="FG",
        contract="FG2701",
        observed_at=at,
        available_at=at,
        artifact_sha256="a" * 64,
        source="https://www.czce.com.cn",
        price=950,
        daily_status="degraded",
        daily_bars=[
            bar | {"date": "2026-11-17", "hold": None},
            bar | {"date": "2026-11-18", "completed": False},
            bar | {"date": "2026-11-19"},
            bar | {"date": "2026-11-20"},
        ],
    )
    context = live_observation_context(published, at)
    assert context["daily_status"] == "degraded"
    assert context["reaction_history"] == [
        {
            "contract": "FG2701",
            "day": "2026-11-19",
            "close": 950,
            "volume": 112,
            "open_interest": 700,
            "source_id": "a" * 64,
        }
    ]
