import { describe, expect, it } from "vitest";
import { directionLabel, normalizeDashboard } from "./model";

describe("normalizeDashboard", () => {
  it("keeps a post-night-close partial daily bar explicit", () => {
    const result = normalizeDashboard({
      generated_at: "2026-09-14T23:05:00+08:00",
      latest_strategy: {
        product: "RU",
        contract: "RU2701",
        action: "long",
        trigger: { condition: "underlying_at_or_above", price: 16000 },
        target: 16600,
        invalidation: 15740,
      },
      evidence: [
        {
          domain: "technical_structure",
          availability: "partial",
          freshness: "fresh",
          integrity: "verified",
          facts: {
            session_daily_bar: {
              bar_state: "session_partial",
              as_of: "2026-09-14T23:05:00+08:00",
              coverage: {
                session_kind: "night",
                schedule_group: "23:00",
                checkpoint_at: "2026-09-14T23:00:00+08:00",
                observed_through: "2026-09-14T23:00:00+08:00",
                expected_daily_close_at: "2026-09-15T15:00:00+08:00",
                covered_trading_seconds: 7200,
              },
              source: {
                kind: "url",
                locator: "https://example.com/RU2701",
                retrieved_at: "2026-09-14T23:05:00+08:00",
                content_sha256: "a".repeat(64),
              },
              calendar_evidence: {
                exchange: "SHFE",
                trade_date: "2026-09-15",
                regular_session_confirmed: true,
                url: "https://www.shfe.com.cn/services/tradingdata/calendar/",
                retrieved_at: "2026-09-14T23:04:00+08:00",
                locator: "2026-09-15 常规交易日",
              },
            },
          },
        },
      ],
    });

    expect(result.strategy).toMatchObject({ product: "RU", contract: "RU2701", direction: "long" });
    expect(result.strategy?.trigger).toBe("≥ 16000");
    expect(result.bar).toEqual({
      state: "partial_night",
      rawState: "session_partial",
      asOf: "2026-09-14T23:05:00+08:00",
      coverage: expect.stringContaining("夜盘组=23:00"),
      source: expect.stringContaining("定位=https://example.com/RU2701"),
      calendarEvidence: expect.stringContaining("交易所=SHFE"),
    });
  });

  it("does not invent a strategy from an empty snapshot", () => {
    expect(normalizeDashboard({ architecture: { nodes: [] }, evidence: [] }).strategy).toBeNull();
  });

  it("combines the API envelope with its domain projection", () => {
    const result = normalizeDashboard({
      schema_version: "dashboard_api.v1",
      system: { status: "ready" },
      codex: { authenticated: true },
      latest_strategy: {
        schema_version: "latest_strategy_record.v1",
        run_id: "run-1",
        strategy: {
          schema_version: "latest_strategy.v1",
          decision: "long_call",
          product: "RB",
          analysis_contract: "RB2701",
          direction: "long",
          session_daily_bar: {
            bar_state: "session_partial",
            observed_at: "2026-09-14T23:05:00+08:00",
            coverage: { session_kind: "night", schedule_group: "23:00" },
            source: { kind: "url", locator: "https://example.com/RB2701" },
          },
          market_levels: { current_price: 3520, strongest_support: 3480, strongest_resistance: 3560, trigger: 3550, invalidation: 3470, target: 3650 },
          rationale: { thesis: "等待向上触发。", why_now: "接近压力。", strongest_counterevidence: "夜盘未收。" },
          option_expression: { route: "long_call", symbol: "RB2701-C-3600" },
          option_market: { status: "complete", freshness: "fresh", conclusion: "报价可执行。", sources: [] },
          technical_structure: { status: "partial", freshness: "fresh", conclusion: "结构待触发。", sources: [] },
          fundamentals_news: { status: "complete", freshness: "fresh", conclusion: "边际改善。", sources: [] },
          member_flow: { status: "partial", freshness: "fresh", conclusion: "多头集中度回升。", sources: [] },
          risks: ["夜盘可能反转"],
          reassess_by: "2026-09-15T15:00:00+08:00"
        },
      },
      domain_snapshot: {
        architecture: {
          nodes: [{ id: "intent_router", label: "任务路由", owner: "deterministic", status: "ready", summary: "识别任务" }],
          edges: [],
        },
        universe: { product_count: 22 },
        evidence: [],
      },
    });

    expect(result.productCount).toBe(22);
    expect(result.nodes[0]).toMatchObject({ id: "intent_router", label: "任务路由", status: "ready" });
    expect(result.systemState).toBe("ready");
    expect(result.codexState).toBe("ready");
    expect(result.strategy).toMatchObject({
      product: "RB",
      contract: "RB2701",
      direction: "long",
      trigger: "3550",
      currentPrice: "3520",
      optionAction: "buy_to_open",
      optionRoute: "long_call",
      optionContract: "RB2701-C-3600",
    });
    expect(result.evidence).toHaveLength(4);
    expect(result.bar.state).toBe("partial_night");
  });

  it("preserves the backend freshness state", () => {
    const result = normalizeDashboard({
      latest_strategy: null,
      latest_strategy_status: {
        state: "stale",
        reason: "session_partial_daily_close_elapsed",
      },
    });

    expect(result.strategyState).toBe("stale");
    expect(result.strategyStateReason).toBe("session_partial_daily_close_elapsed");
  });
});

it("keeps a backend session_partial bar visibly partial after the night close", () => {
  const dashboard = normalizeDashboard({
    latest_strategy: {
      product: "MA",
      analysis_contract: "MA2701",
      direction: "long",
      decision: "wait",
      session_daily_bar: {
        bar_state: "session_partial",
        as_of: "2026-09-14T01:05:00+08:00",
        coverage: {
          session_kind: "night",
          schedule_group: "23:00",
          observed_through: "2026-09-13T23:00:00+08:00",
        },
      },
    },
  });

  expect(dashboard.bar.state).toBe("partial_night");
  expect(dashboard.bar.coverage).toContain("23:00");
  expect(dashboard.bar.source).toBe("");
});

describe("directionLabel", () => {
  it("uses decision language instead of protocol labels", () => {
    expect(directionLabel("long")).toBe("偏多");
    expect(directionLabel("wait")).toBe("等待");
  });
});
