from datetime import date

from app import rolling_calendar as calendar


def test_extended_calendar_survives_restart_and_failed_refresh(tmp_path, monkeypatch):
    old = ("2026-12-30", "2026-12-31")
    monkeypatch.setattr(calendar, "_bundled", lambda: old)

    class Published:
        def raise_for_status(self):
            pass

        def json(self):
            return ["20261230", "20261231", "20270104"]

    monkeypatch.setattr(calendar.requests, "get", lambda *a, **kw: Published())
    calendar.refresh_calendar(tmp_path, date(2026, 12, 20))
    assert calendar.trading_days(tmp_path) == old + ("2027-01-04",)

    def offline(*args, **kwargs):
        raise calendar.requests.ConnectionError

    monkeypatch.setattr(calendar.requests, "get", offline)
    calendar.refresh_calendar(tmp_path, date(2027, 1, 2))
    assert calendar.trading_days(tmp_path) == old + ("2027-01-04",)
