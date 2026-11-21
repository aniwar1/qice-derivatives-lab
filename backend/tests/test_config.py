from __future__ import annotations

import pytest

from app.config import Settings


def test_codex_start_timeout_defaults_to_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CFDA_CODEX_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CFDA_CODEX_START_TIMEOUT_SECONDS", raising=False)

    settings = Settings.from_env()

    assert settings.codex_timeout_seconds == 900
    assert settings.codex_start_timeout_seconds == 30


def test_codex_start_timeout_is_configurable_below_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CFDA_CODEX_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("CFDA_CODEX_START_TIMEOUT_SECONDS", "45")

    settings = Settings.from_env()

    assert settings.codex_timeout_seconds == 120
    assert settings.codex_start_timeout_seconds == 45


def test_codex_start_timeout_cannot_exceed_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CFDA_CODEX_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("CFDA_CODEX_START_TIMEOUT_SECONDS", "31")

    with pytest.raises(
        ValueError,
        match=(
            "CFDA_CODEX_START_TIMEOUT_SECONDS must not exceed "
            "CFDA_CODEX_TIMEOUT_SECONDS"
        ),
    ):
        Settings.from_env()
