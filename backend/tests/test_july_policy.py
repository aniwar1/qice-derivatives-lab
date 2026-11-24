from app.july_policy import eligible_event


def test_no_event_reuse_requires_an_unavailable_pair_or_an_inapplicable_schedule():
    packet = {
        "next_sessions": ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"],
        "products": {"MA": {"options": [{"side": "C"}]}, "CF": {"options": []}},
        "documents": [{"scheduled_at": "2026-07-01T11:00:00+08:00", "products": ["MA"]}],
    }
    assert not eligible_event(packet)
    packet["products"]["MA"]["options"].append({"side": "P"})
    assert eligible_event(packet)
    packet["documents"][0]["products"] = ["CF"]
    assert not eligible_event(packet)
    packet["documents"][0].update(products=["MA"], scheduled_at="2026-07-01T09:05:00+08:00")
    assert not eligible_event(packet)
    packet["documents"][0]["scheduled_at"] = "2026-07-07T15:01:00+08:00"
    assert not eligible_event(packet)
