import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { createStrategyRun, getDashboard, getStrategyRun, subscribeToRun } from "./lib/api";

vi.mock("./lib/api", () => ({
  getDashboard: vi.fn(),
  getResearchRuns: vi.fn().mockResolvedValue([]),
  getStrategyRun: vi.fn(),
  createStrategyRun: vi.fn(),
  subscribeToRun: vi.fn(),
}));

const dashboardFixture = {
  generated_at: "2026-09-14T23:05:00+08:00",
  latest_strategy: {
    product: "RU",
    contract: "RU2701",
    action: "long",
    path: "夜盘结构走强，等待价格触发。",
    trigger: 16000,
    target: 16600,
    invalidation: 15740,
    why_now: "波动收敛后资金重新流入。",
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
          },
          source: { kind: "url", locator: "https://example.com/RU2701" },
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
  architecture: {
    nodes: [
      { id: "agent_hypothesis", label: "主假设", owner: "agent", status: "complete", summary: "选择一个可证伪假设。" },
    ],
    edges: [],
  },
  universe: { product_count: 22 },
  system: { status: "healthy" },
};

describe("App", () => {
  beforeEach(() => {
    vi.mocked(getDashboard).mockReset();
    vi.mocked(getStrategyRun).mockReset();
    vi.mocked(createStrategyRun).mockReset();
    vi.mocked(subscribeToRun).mockReset();
    vi.mocked(getStrategyRun).mockResolvedValue({ run_id: "test-run", status: "running" });
    vi.mocked(subscribeToRun).mockReturnValue(vi.fn());
  });

  it("puts the latest strategy first and labels an in-session daily bar", async () => {
    vi.mocked(getDashboard).mockResolvedValue(dashboardFixture);
    render(<App />);

    expect(await screen.findByRole("heading", { name: /RU\s*偏多/ })).toBeInTheDocument();
    expect(screen.getByText("夜盘收盘后部分日线")).toBeInTheDocument();
    expect(screen.getByText("夜盘结构走强，等待价格触发。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "更新最新策略" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "解释日线状态" })).toHaveAccessibleDescription(
      expect.stringContaining("交易日历=交易所=SHFE"),
    );
  });

  it("offers a recovery action when the local API is unavailable", async () => {
    vi.mocked(getDashboard).mockRejectedValue(new Error("连接被拒绝"));
    render(<App />);

    expect(await screen.findByRole("heading", { name: "暂时无法读取策略" })).toBeInTheDocument();
    expect(screen.getByText("连接被拒绝")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新连接" })).toBeEnabled();
  });

  it("reattaches to an automatically started backend run", async () => {
    vi.mocked(getDashboard).mockResolvedValue({
      ...dashboardFixture,
      latest_strategy: null,
      active_run: { run_id: "auto-run-1", status: "running" },
    });
    render(<App />);

    expect(await screen.findByRole("button", { name: "策略更新中…" })).toBeDisabled();
    expect(screen.getAllByText("Codex 正在更新策略")).not.toHaveLength(0);
    expect(vi.mocked(subscribeToRun)).toHaveBeenCalledWith(
      expect.objectContaining({
        run_id: "auto-run-1",
        events_url: "/api/runs/auto-run-1/events",
      }),
      expect.any(Function),
      expect.any(Function),
    );
  });

  it("keeps a legacy stale snapshot explainable until manual refresh", async () => {
    vi.mocked(getDashboard).mockResolvedValue({
      ...dashboardFixture,
      latest_strategy: null,
      latest_strategy_status: {
        state: "stale",
        reason: "session_partial_daily_close_elapsed",
      },
    });
    render(<App />);

    expect(await screen.findByRole("heading", { name: "等待手动更新" })).toBeInTheDocument();
    expect(screen.getByText("上一版策略不会自动隐藏；需要新证据时，点击右上角按钮手动更新。")).toBeInTheDocument();
  });

  it("uses the task-status poll to finish when the progress stream has no terminal event", async () => {
    vi.mocked(getDashboard).mockResolvedValue(dashboardFixture);
    vi.mocked(createStrategyRun).mockResolvedValue({
      run_id: "manual-run-1",
      status: "queued",
      events_url: "/api/runs/manual-run-1/events",
    });
    vi.mocked(getStrategyRun).mockResolvedValue({
      run_id: "manual-run-1",
      status: "completed",
      result: null,
    });
    render(<App />);

    const updateButtons = await screen.findAllByRole("button", { name: "更新最新策略" });
    const update = updateButtons[updateButtons.length - 1];
    fireEvent.click(update);

    await waitFor(() =>
      expect(vi.mocked(createStrategyRun)).toHaveBeenCalledOnce(),
    );
    await waitFor(() => expect(vi.mocked(getStrategyRun)).toHaveBeenCalledWith("manual-run-1"));
    expect(vi.mocked(getStrategyRun)).toHaveBeenCalledWith("manual-run-1");
  });
});
