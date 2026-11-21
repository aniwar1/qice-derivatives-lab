from datetime import date
import json
import pytest

from app.agent_replay import digest
from app.study_view import read_context, read_results


def test_context_is_bound_to_the_day_and_frozen_digest(tmp_path):
    assert read_results(tmp_path)["status"] == "missing"
    day = date(2026, 5, 21)
    packet = {"trade_date": str(day), "cutoff": "2026-05-20T20:00:00+08:00"}
    key = "a" * 64
    path = tmp_path / "receipts" / key / "context.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(packet))
    row = {
        "day": str(day),
        "cutoff": packet["cutoff"],
        "receipt_key": key,
        "context_sha256": digest(packet),
    }
    report = {
        "status": "partial",
        "schema_version": 1,
        "plans": [{"id": group, "sessions": [row]} for group in "ABC"],
    }
    (tmp_path / "results.json").write_text(json.dumps(report))
    assert read_context(tmp_path, day) == packet
    assert read_context(tmp_path, date(2026, 5, 22)) is None
    path.write_text(json.dumps(packet | {"extra": "modified"}))
    with pytest.raises(ValueError):
        read_context(tmp_path, day)


def test_source_download_requires_reference_in_that_day_and_valid_original(
    tmp_path, monkeypatch
):
    from hashlib import sha256
    from app import study_view

    content = b"issuer publication"
    key = sha256(content).hexdigest()
    row = {"sha256": key, "url": "https://www.stats.gov.cn/publication.html"}
    monkeypatch.setattr(
        study_view, "read_context", lambda directory, day: {"documents": [row]}
    )
    raw = tmp_path / "source-snapshot" / "blobs" / key
    raw.parent.mkdir(parents=True)
    raw.write_bytes(content)
    day = date(2026, 5, 21)
    assert study_view.read_document(tmp_path, day, key) == (content, ".html")
    assert study_view.read_document(tmp_path, day, "4" * 64) is None
    raw.write_bytes(b"changed")
    with pytest.raises(ValueError):
        study_view.read_document(tmp_path, day, key)
