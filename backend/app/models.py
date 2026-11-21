from __future__ import annotations

from datetime import date, datetime
from enum import Enum
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


SENSITIVE_URL_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "signature",
    "token",
    "x-amz-credential",
    "x-amz-signature",
}

OFFICIAL_EXCHANGE_DOMAINS = {
    "SHFE": "shfe.com.cn",
    "INE": "ine.cn",
    "DCE": "dce.com.cn",
    "CZCE": "czce.com.cn",
    "GFEX": "gfex.com.cn",
}
# 交易所映射始终由当前品种库提供，避免删除后仍沿用静态范围。
PRODUCT_EXCHANGES = {
    row["symbol"]: row["exchange"]
    for row in json.loads(
        (Path(__file__).resolve().parents[2] / "config/trading_universe.json").read_text(encoding="utf-8")
    )["permission_scope"]["allowed_products"]
}
CALENDAR_EVIDENCE_MAX_AGE_SECONDS = 86_400


def _url_contains_credentials(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        return True
    return any(
        key.lower() in SENSITIVE_URL_KEYS
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    )


def _product_prefix(contract: str) -> str:
    return contract.rstrip("0123456789")


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return value


class RunStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class StrategyRunRequest(StrictModel):
    mode: Literal["all_market_scan", "single_analysis", "option_entry"] = "all_market_scan"
    product: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}$")
    requested_contract: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}[0-9]{4}$")
    option_analysis_requested: bool = True

    @field_validator("product", "requested_contract", mode="before")
    @classmethod
    def uppercase_symbols(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_mode_scope(self) -> "StrategyRunRequest":
        if self.mode == "all_market_scan" and (self.product or self.requested_contract):
            raise ValueError("all_market_scan cannot be narrowed to one product or contract")
        if self.mode in {"single_analysis", "option_entry"} and not self.product:
            raise ValueError(f"{self.mode} requires product")
        if self.requested_contract and self.product and _product_prefix(self.requested_contract) != self.product:
            raise ValueError("requested_contract conflicts with product")
        if self.mode == "option_entry" and not self.option_analysis_requested:
            raise ValueError("option_entry requires option analysis")
        return self


class SourceReference(StrictModel):
    url: HttpUrl
    title: str = Field(min_length=1, max_length=240)
    retrieved_at: datetime
    locator: str = Field(min_length=1, max_length=500)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "retrieved_at")

    @model_validator(mode="after")
    def url_has_no_embedded_credentials(self) -> "SourceReference":
        if _url_contains_credentials(str(self.url)):
            raise ValueError("source URL cannot contain credentials")
        return self


class EvidenceDomainSummary(StrictModel):
    status: Literal["complete", "partial", "unobservable"]
    freshness: Literal["fresh", "stale", "not_applicable"]
    conclusion: str = Field(min_length=1, max_length=1200)
    sources: list[SourceReference] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def observable_evidence_has_a_source(self) -> "EvidenceDomainSummary":
        if self.status in {"complete", "partial"} and not self.sources:
            raise ValueError("observable evidence requires at least one source")
        return self


class ScanProductObservation(StrictModel):
    product: str = Field(pattern=r"^[A-Z]{1,3}$")
    status: Literal["complete", "partial", "unobservable"]
    actual_main_contract: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}[0-9]{4}$")
    observed_at: datetime | None = None
    known_gap: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("product", "actual_main_contract", mode="before")
    @classmethod
    def normalize_identity(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "observed_at") if value is not None else None

    @model_validator(mode="after")
    def status_matches_identity(self) -> "ScanProductObservation":
        if self.actual_main_contract and _product_prefix(self.actual_main_contract) != self.product:
            raise ValueError("scan-row contract conflicts with product")
        if self.status in {"complete", "partial"} and (
            self.actual_main_contract is None or self.observed_at is None
        ):
            raise ValueError("an observable scan row requires contract and observation time")
        if self.status == "unobservable" and not self.known_gap:
            raise ValueError("an unobservable scan row requires a known gap")
        return self


class ScanCoverage(StrictModel):
    scope: Literal["all_market", "single_product"]
    expected_count: int = Field(gt=0, le=100)
    attempted_count: int = Field(gt=0, le=100)
    products: list[ScanProductObservation] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def counts_are_exact(self) -> "ScanCoverage":
        symbols = [item.product for item in self.products]
        if len(symbols) != len(set(symbols)):
            raise ValueError("scan coverage contains duplicate products")
        if self.expected_count != len(symbols) or self.attempted_count != len(symbols):
            raise ValueError("scan coverage counts must equal the explicit product rows")
        return self


class SessionBarOHLCV(StrictModel):
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float | None = Field(default=None, ge=0)
    open_interest: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ohlc_order_is_possible(self) -> "SessionBarOHLCV":
        if self.high < max(self.open, self.low, self.close) or self.low > min(self.open, self.high, self.close):
            raise ValueError("session OHLC ordering is impossible")
        return self


class SessionBarCoverage(StrictModel):
    session_kind: Literal["night", "day"]
    schedule_group: Literal["NONE", "23:00", "01:00", "02:30"]
    session_started_at: datetime
    checkpoint_at: datetime
    observed_through: datetime
    expected_daily_close_at: datetime
    covered_trading_seconds: int = Field(ge=0)
    required_checkpoint_seconds: int = Field(gt=0)

    @field_validator(
        "session_started_at",
        "checkpoint_at",
        "observed_through",
        "expected_daily_close_at",
    )
    @classmethod
    def timestamps_are_aware(cls, value: datetime, info: Any) -> datetime:
        return _aware(value, info.field_name)


class SessionBarSource(StrictModel):
    kind: Literal["url", "file"]
    locator: str = Field(min_length=1, max_length=2048)
    retrieved_at: datetime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "retrieved_at")

    @model_validator(mode="after")
    def url_locator_has_no_embedded_credentials(self) -> "SessionBarSource":
        if self.kind == "url" and _url_contains_credentials(self.locator):
            raise ValueError("session source URL cannot contain credentials")
        return self


class TradingCalendarEvidence(StrictModel):
    exchange: Literal["SHFE", "INE", "DCE", "CZCE", "GFEX"]
    trade_date: date
    regular_session_confirmed: Literal[True]
    url: HttpUrl
    retrieved_at: datetime
    locator: str = Field(min_length=1, max_length=1000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "calendar evidence retrieved_at")

    @model_validator(mode="after")
    def url_is_public_official_exchange_domain(self) -> "TradingCalendarEvidence":
        raw_url = str(self.url)
        parsed = urlsplit(raw_url)
        hostname = (parsed.hostname or "").lower()
        expected_domain = OFFICIAL_EXCHANGE_DOMAINS[self.exchange]
        if (
            parsed.scheme != "https"
            or not (
                hostname == expected_domain
                or hostname.endswith(f".{expected_domain}")
            )
            or _url_contains_credentials(raw_url)
        ):
            raise ValueError(
                "calendar evidence must use its exchange's public official HTTPS domain"
            )
        return self


class SessionBarClaimBoundary(StrictModel):
    completed_bar_claim_allowed: bool
    confirmed_daily_breakout_claim_eligible: bool


class SessionDailyBar(StrictModel):
    schema_version: Literal["session_aware_daily_bar.v1"]
    product: str = Field(pattern=r"^[A-Z]{1,3}$")
    analysis_contract: str = Field(pattern=r"^[A-Z]{1,3}[0-9]{4}$")
    trade_date: date
    bar_state: Literal["session_partial", "completed"]
    as_of: datetime
    decision_semantics: Literal["anticipatory", "conditional", "confirmed_daily_breakout"]
    ohlcv: SessionBarOHLCV
    coverage: SessionBarCoverage
    source: SessionBarSource
    calendar_evidence: TradingCalendarEvidence | None
    claim_boundary: SessionBarClaimBoundary

    @field_validator("product", "analysis_contract", mode="before")
    @classmethod
    def normalize_identity(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "as_of")

    @model_validator(mode="after")
    def bar_claims_match_state(self) -> "SessionDailyBar":
        if _product_prefix(self.analysis_contract) != self.product:
            raise ValueError("session bar contract conflicts with product")
        if self.source.retrieved_at > self.as_of:
            raise ValueError("session bar source retrieval cannot exceed as_of")
        if self.coverage.observed_through > self.as_of:
            raise ValueError("session coverage cannot exceed as_of")
        if self.bar_state == "session_partial":
            if self.decision_semantics not in {"anticipatory", "conditional"}:
                raise ValueError("a partial session bar cannot claim confirmed breakout")
            if self.calendar_evidence is None:
                raise ValueError("a partial session bar requires official calendar evidence")
            expected_exchange = PRODUCT_EXCHANGES.get(self.product)
            if expected_exchange is None or self.calendar_evidence.exchange != expected_exchange:
                raise ValueError("calendar evidence exchange conflicts with product")
            if self.calendar_evidence.trade_date != self.trade_date:
                raise ValueError("calendar evidence trade_date conflicts with session bar")
            if self.calendar_evidence.retrieved_at > self.as_of:
                raise ValueError("calendar evidence retrieval cannot exceed as_of")
            calendar_age = (self.as_of - self.calendar_evidence.retrieved_at).total_seconds()
            if calendar_age > CALENDAR_EVIDENCE_MAX_AGE_SECONDS:
                raise ValueError("calendar evidence is too old for a partial-bar decision")
            expected = (False, False)
        else:
            if self.calendar_evidence is not None:
                raise ValueError("a completed session bar requires calendar_evidence=null")
            expected = (True, True)
        supplied = (
            self.claim_boundary.completed_bar_claim_allowed,
            self.claim_boundary.confirmed_daily_breakout_claim_eligible,
        )
        if supplied != expected:
            raise ValueError("session bar claim boundary conflicts with bar_state")
        return self


class MarketLevels(StrictModel):
    current_price: float | None = Field(default=None, gt=0)
    quote_at: datetime | None = None
    strongest_support: float | None = Field(default=None, gt=0)
    strongest_resistance: float | None = Field(default=None, gt=0)
    trigger: float | None = Field(default=None, gt=0)
    invalidation: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    tick_size: float | None = Field(default=None, gt=0)

    @field_validator("quote_at")
    @classmethod
    def quote_at_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value, "quote_at") if value is not None else None


class StrategyRationale(StrictModel):
    thesis: str = Field(min_length=1, max_length=1600)
    why_now: str = Field(min_length=1, max_length=1000)
    why_not_wait: str = Field(min_length=1, max_length=1000)
    why_not_opposite: str = Field(min_length=1, max_length=1000)
    strongest_counterevidence: str = Field(min_length=1, max_length=1000)


class OptionExpressionSummary(StrictModel):
    route: Literal["long_call", "long_put"]
    symbol: str = Field(min_length=1, max_length=80)
    underlying_contract: str = Field(pattern=r"^[A-Z]{1,3}[0-9]{4}$")
    expiration_date: date
    strike: float = Field(gt=0)
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    quote_at: datetime
    implied_volatility: float | None = Field(default=None, gt=0)
    delta: float | None = Field(default=None, ge=-1, le=1)
    theta: float | None = None
    max_premium_loss: float | None = Field(default=None, gt=0)

    @field_validator("quote_at")
    @classmethod
    def quote_at_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "option quote_at")

    @model_validator(mode="after")
    def executable_quote_is_not_crossed(self) -> "OptionExpressionSummary":
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("option bid cannot exceed ask")
        return self


class BoundedQualityReview(StrictModel):
    """Exactly one resolved challenge to the Agent's preferred hypothesis."""

    critic_revisions: Literal[1]
    strongest_challenge: str = Field(min_length=1, max_length=1000)
    resolution: str = Field(min_length=1, max_length=1000)
    unresolved_material_issue: Literal[None]

    @field_validator("strongest_challenge", "resolution", mode="before")
    @classmethod
    def review_text_is_explicit(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class EvidenceAssessment(StrictModel):
    assessment: str = Field(min_length=1, max_length=850)
    source_ids: list[str] = Field(max_length=8)


class OwnershipAssessment(StrictModel):
    classification: Literal["unknown", "exchange_member_aggregate"]
    source_ids: list[str] = Field(default_factory=list, max_length=2)
    assessment: str = Field(min_length=1, max_length=400)


class SleevePlan(StrictModel):
    strategy: Literal["A", "B", "C"]
    action: Literal["wait", "paper_entry"]
    reason: str = Field(min_length=1, max_length=850)
    quantity: int = Field(ge=0)
    max_holding_days: int = Field(ge=0, le=5)
    legs: list[str] = Field(max_length=2)
    comparison_symbols: list[str] = Field(default_factory=list, max_length=4)
    scenario_low: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    scenario_high: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    event_source_id: str | None
    event_at: datetime | None
    invalidation: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def check_plan(self) -> "SleevePlan":
        if self.action == "wait":
            if self.quantity != 0 or self.legs or self.max_holding_days:
                raise ValueError("等待须为零仓位、零持有期且不包含建仓腿")
        elif not self.quantity or not self.max_holding_days or len(self.legs) != (2 if self.strategy == "C" else 1):
            raise ValueError("纸面计划的腿数、数量或期限无效")
        if self.event_at is not None:
            _aware(self.event_at, "event_at")
        return self


class AgentReview(StrictModel):
    technical: EvidenceAssessment
    fundamentals: EvidenceAssessment
    news: EvidenceAssessment
    positioning: OwnershipAssessment
    plans: list[SleevePlan] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def check_strategies(self) -> "AgentReview":
        if {plan.strategy for plan in self.plans} != {"A", "B", "C"}:
            raise ValueError("每轮研究必须分别评估 A、B、C")
        return self


class AgentStrategyPayload(StrictModel):
    schema_version: Literal["codex_strategy_decision.v1"]
    as_of: datetime
    decision: Literal["futures", "long_call", "long_put", "wait"]
    product: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}$")
    product_name: str | None = Field(default=None, min_length=1, max_length=80)
    actual_main_contract: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}[0-9]{4}$")
    analysis_contract: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}[0-9]{4}$")
    direction: Literal["long", "short", "neutral", "uncertain"]
    entry_style: Literal[
        "anticipatory_breakout",
        "confirmed_breakout",
        "trend_pullback",
        "structure_reversal",
        "event_preposition",
        "post_event_continuation",
        "none",
    ]
    entry_timing: Literal["now", "conditional", "wait"]
    scan_coverage: ScanCoverage
    session_daily_bar: SessionDailyBar | None
    market_levels: MarketLevels
    rationale: StrategyRationale
    agent_review: AgentReview | None = None
    technical_structure: EvidenceDomainSummary
    fundamentals_news: EvidenceDomainSummary
    member_flow: EvidenceDomainSummary
    option_market: EvidenceDomainSummary
    option_expression: OptionExpressionSummary | None = None
    risks: list[str] = Field(min_length=1, max_length=12)
    evidence_gaps: list[str] = Field(default_factory=list, max_length=20)
    quality_review: BoundedQualityReview
    reassess_by: datetime
    order_placement_authorized: Literal[False]
    short_option_open_allowed: Literal[False]

    @field_validator("product", "actual_main_contract", "analysis_contract", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("as_of", "reassess_by")
    @classmethod
    def decision_timestamps_are_aware(cls, value: datetime, info: Any) -> datetime:
        return _aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_strategy_semantics(self) -> "AgentStrategyPayload":
        if self.reassess_by <= self.as_of:
            raise ValueError("reassess_by must be after as_of")
        if self.product is None and any(
            value is not None
            for value in (
                self.product_name,
                self.actual_main_contract,
                self.analysis_contract,
                self.session_daily_bar,
            )
        ):
            raise ValueError("a product-less decision cannot carry product-bound identity")
        if self.session_daily_bar is not None and (
            self.session_daily_bar.product != self.product
            or self.session_daily_bar.analysis_contract != self.analysis_contract
        ):
            raise ValueError("session bar identity conflicts with strategy identity")
        if self.entry_style == "confirmed_breakout" and (
            self.session_daily_bar is None
            or not self.session_daily_bar.claim_boundary.confirmed_daily_breakout_claim_eligible
        ):
            raise ValueError("confirmed_breakout requires a completed confirmation-eligible daily bar")
        if self.decision == "long_call" and self.direction != "long":
            raise ValueError("long_call requires a long underlying view")
        if self.decision == "long_put" and self.direction != "short":
            raise ValueError("long_put requires a short underlying view")
        if self.decision == "futures" and self.direction not in {"long", "short"}:
            raise ValueError("a futures action requires a long or short direction")
        if self.decision == "wait":
            if self.entry_timing != "wait":
                raise ValueError("wait decision requires wait timing")
        elif self.entry_timing == "wait":
            raise ValueError("a capital action cannot use wait timing")
        if self.decision in {"long_call", "long_put"}:
            if self.option_expression is None or self.option_expression.route != self.decision:
                raise ValueError("option decision requires a matching option expression")
        elif self.option_expression is not None:
            raise ValueError("option_expression is only allowed for a long option decision")
        if self.decision != "wait":
            if not self.product or not self.actual_main_contract or not self.analysis_contract:
                raise ValueError(
                    "a capital action requires product, actual_main_contract and analysis_contract"
                )
            if self.session_daily_bar is None:
                raise ValueError("a capital action requires a validated session-aware daily bar")
            if self.market_levels.current_price is None or self.market_levels.quote_at is None:
                raise ValueError("a capital action requires a current underlying quote")
            if any(
                value is None
                for value in (
                    self.market_levels.trigger,
                    self.market_levels.invalidation,
                    self.market_levels.target,
                )
            ):
                raise ValueError("a capital action requires trigger, invalidation and target")
            if self.market_levels.strongest_support is None or self.market_levels.strongest_resistance is None:
                raise ValueError("a capital action requires one strongest support and resistance")
            trigger = self.market_levels.trigger
            invalidation = self.market_levels.invalidation
            target = self.market_levels.target
            assert trigger is not None and invalidation is not None and target is not None
            if self.direction == "long" and not invalidation < trigger < target:
                raise ValueError("long levels must satisfy invalidation < trigger < target")
            if self.direction == "short" and not target < trigger < invalidation:
                raise ValueError("short levels must satisfy target < trigger < invalidation")
        if self.product and self.analysis_contract and _product_prefix(self.analysis_contract) != self.product:
            raise ValueError("analysis_contract conflicts with product")
        if self.product and self.actual_main_contract and _product_prefix(self.actual_main_contract) != self.product:
            raise ValueError("actual_main_contract conflicts with product")
        if self.option_expression and self.analysis_contract:
            if self.option_expression.underlying_contract != self.analysis_contract:
                raise ValueError("option underlying contract must equal analysis_contract")
        return self


class CodexRuntimeMetadata(StrictModel):
    provider: Literal["codex"] = "codex"
    transport: Literal["python_sdk_stdio", "cli_exec_json"]
    auth_mode: Literal["chatgpt_managed", "api_key", "unauthenticated", "unknown"]
    authenticated: bool
    model: str
    model_resolution: Literal["explicit", "inherited"]
    version: str | None = None
    browser_direct_access: Literal[False] = False
    credentials_exposed: Literal[False] = False


class LatestStrategyRecord(StrictModel):
    schema_version: Literal["latest_strategy_record.v1"] = "latest_strategy_record.v1"
    run_id: str
    updated_at: datetime
    source: CodexRuntimeMetadata
    strategy: AgentStrategyPayload


class RunEvent(StrictModel):
    event_id: int
    run_id: str
    type: Literal["snapshot", "progress", "completed", "failed", "heartbeat"]
    stage: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class StrategyRunAccepted(StrictModel):
    run_id: str
    status: RunStatus
    events_url: str


class StrategyRunView(StrictModel):
    run_id: str
    status: RunStatus
    request: StrategyRunRequest
    result: LatestStrategyRecord | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime
