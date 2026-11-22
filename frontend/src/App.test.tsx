import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import {
  createStrategyRun,
  getDashboard,
  getIntradayReference,
  getProductAnalysis,
  getStrategyRun,
  getRollingBaseline,
  subscribeToRun,
} from "./lib/api";
import type {
  RollingBaseline,
  RollingPlan,
  RollingPoint,
  StrategyGroup,
} from "./types";

vi.mock("./lib/api", () => ({
  getDashboard: vi.fn(),
  getAgentStudy: vi.fn().mockResolvedValue({ status: "missing", plans: [] }),
  getResearchRuns: vi.fn().mockResolvedValue([]),
  getRollingBaseline: vi.fn(),
  getIntradayReference: vi.fn(),
  getProductAnalysis: vi.fn(),
  getStrategyRun: vi.fn(),
  createStrategyRun: vi.fn(),
  subscribeToRun: vi.fn(),
}));

const day = "2026-11-20";
const baselineDay = "2026-05-22";
const point = (date: string, value: number): RollingPoint => ({
  day: date,
  equity: value,
  raw_equity: value,
  index_value: value / 2000,
  return_pct: (value / 200000 - 1) * 100,
  source: "historical",
  open_positions: 1,
  stale_marks: [],
});
const planFixture = (id: StrategyGroup): RollingPlan => ({
  id,
  variant: "baseline",
  baseline_day: baselineDay,
  baseline_equity: 200000,
  baseline_point: point(baselineDay, 200000),
  latest_day: day,
  curve: [point(day, 201000)],
  stats: {
    equity: 201000,
    return_pct: 0.5,
    net_pnl: 1000,
    raw_net_pnl: 1000,
    drawdown_pct: 2,
    opened: 1,
    closed: 0,
    open_positions: 1,
  },
  sessions: [
    {
      day,
      source: "historical",
      cutoff: "2026-11-19T20:30:00+08:00",
      decision: {
        product: "JD",
        action: "enter",
        technical: "同月收盘上行",
        news: "暂无公告",
        legs: ["JD2701-C-4000"],
      },
      execution: {
        status: "simulated_open",
        quantity: 1,
        premium_and_fees: 303,
        exits: [],
      },
    },
  ],
});
const report: RollingBaseline = {
  status: "ready",
  start: baselineDay,
  end: "2026-11-22",
  as_of: "2026-11-22T12:30:00+08:00",
  latest_expected_day: day,
  latest_day: day,
  latest_recorded_at: null,
  freshness: "current",
  missing_days: [],
  historical_batch: "2026-05-21_2026-11-20",
  historical_end: day,
  assumptions: [],
  plans: (["A", "B", "C"] as const).map(planFixture),
};
const dashboard = {
  product_catalog: [
    { symbol: "JD", name: "鸡蛋", exchange: "DCE" },
    { symbol: "RU", name: "天然橡胶", exchange: "SHFE" },
  ],
  system: { status: "healthy" },
};

describe("期策工作台", () => {
  afterEach(cleanup);
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    window.scrollTo = vi.fn();
    vi.resetAllMocks();
    vi.mocked(getDashboard).mockResolvedValue(dashboard);
    vi.mocked(getRollingBaseline).mockResolvedValue(report);
    vi.mocked(getIntradayReference).mockResolvedValue({
      status: "closed",
      observed_at: null,
      base_day: day,
      groups: [],
      note: "周末休市",
    });
    vi.mocked(getProductAnalysis).mockImplementation(async (product) => ({
      product,
      observation: null,
      evidence_run_id: null,
      analysis: null,
    }));
    vi.mocked(getStrategyRun).mockResolvedValue({
      run_id: "test-run",
      status: "running",
    });
    vi.mocked(subscribeToRun).mockReturnValue(vi.fn());
  });

  it("并列展示三套计划并从曲线下钻到期权腿", async () => {
    render(<App />);
    expect(
      await screen.findByRole("heading", { name: "多因子方向买权" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "技术择强买权" })).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "事件驱动宽跨式" }),
    ).toBeVisible();
    const chart = within(
      screen.getByRole("region", { name: "半年组合收益曲线" }),
    );
    fireEvent.click(chart.getByRole("button", { name: "重基权益" }));
    expect(chart.getByRole("button", { name: "重基权益" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: /查看策略 C 计划/ }));
    expect(
      await screen.findByRole("heading", { name: "策略 C · 事件驱动宽跨式" }),
    ).toBeVisible();
    expect(screen.getByText("JD2701-C-4000")).toBeVisible();
    expect(screen.getByText(/信息截止 11\/19/)).toBeVisible();
  });

  it("旧链接转到同一策略与交易日，披露缺口并保持正式净值独立", async () => {
    window.history.replaceState(null, "", "/#plans/B/" + day);
    vi.mocked(getRollingBaseline).mockResolvedValue({
      ...report,
      status: "partial",
      freshness: "behind",
      missing_days: ["2026-11-19"],
      issues: ["当日行情未齐"],
      updater: {
        state: "retrying",
        worker_status: "pending",
        reason: "郑商所期权结算原件暂缺",
        next_session: "2026-11-19",
        last_settled: "2026-11-18",
        last_checked_at: null,
        last_success_at: null,
      },
    });
    vi.mocked(getIntradayReference).mockResolvedValue({
      status: "partial",
      base_day: day,
      observed_at: "2026-11-20T14:00:00+08:00",
      note: "仅供参考",
      groups: [{ id: "B", total_legs: 2, marked_legs: 1, pnl_change: 275 }],
    });
    render(<App />);
    expect(
      await screen.findByRole("heading", { name: "策略 B · 技术择强买权" }),
    ).toBeVisible();
    expect(window.location.hash).toBe("#baseline/B/" + day);
    expect(screen.getByLabelText("选择计划交易日")).toHaveValue(day);
    expect(screen.getByText(/缺少 1 个交易日的日结/)).toBeVisible();
    expect(screen.getByText(/后台更新 · 郑商所期权结算原件暂缺/)).toBeVisible();
    const reference = within(screen.getByRole("region", { name: "盘中参考" }));
    expect(reference.getByText(/275 元/)).toBeVisible();
    expect(
      within(
        screen.getByRole("region", { name: "半年组合收益曲线" }),
      ).getAllByText("+0.50%"),
    ).toHaveLength(3);
  });

  it("交易时段三组均空仓时显示状态，不添加参考估值", async () => {
    vi.mocked(getIntradayReference).mockResolvedValue({
      status: "flat",
      base_day: day,
      observed_at: null,
      note: "最近日终没有期权持仓",
      groups: (["A", "B", "C"] as const).map((id) => ({
        id,
        marked_legs: 0,
        total_legs: 0,
        pnl_change: 0,
      })),
    });
    render(<App />);
    const reference = within(screen.getByRole("region", { name: "盘中参考" }));
    expect(await reference.findByText(/各组合空仓/)).toBeVisible();
    expect(reference.queryByRole("list")).toBeNull();
  });

  it("首页按分钟刷新日结和盘中参考，在隐藏及离开页面后停止轮询", async () => {
    vi.useFakeTimers();
    try {
      render(<App />);
      await act(async () => {});
      expect(getIntradayReference).toHaveBeenCalledTimes(1);
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(getRollingBaseline).toHaveBeenCalledTimes(2);
      expect(getIntradayReference).toHaveBeenCalledTimes(2);
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: true,
      });
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await act(() => vi.advanceTimersByTimeAsync(120_000));
      expect(getIntradayReference).toHaveBeenCalledTimes(2);
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: false,
      });
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      expect(getIntradayReference).toHaveBeenCalledTimes(3);
      await act(async () => {
        window.history.replaceState(null, "", "/#products/JD");
        window.dispatchEvent(new HashChangeEvent("hashchange"));
      });
      const baselineReads = vi.mocked(getRollingBaseline).mock.calls.length;
      await act(() => vi.advanceTimersByTimeAsync(120_000));
      expect(getRollingBaseline).toHaveBeenCalledTimes(baselineReads);
      expect(getIntradayReference).toHaveBeenCalledTimes(3);
    } finally {
      Reflect.deleteProperty(document, "hidden");
      vi.useRealTimers();
    }
  });

  it("品种搜索切换专属观察项且保留搜索词", async () => {
    window.history.replaceState(null, "", "/#products/JD");
    render(<App />);
    expect(await screen.findByRole("heading", { name: "鸡蛋" })).toBeVisible();
    expect(screen.getByText(/在产蛋鸡存栏/)).toBeVisible();
    const search = screen.getByRole("textbox", { name: /品种/ });
    fireEvent.change(search, { target: { value: "RU" } });
    fireEvent.click(screen.getByRole("button", { name: /天然橡胶/ }));
    expect(
      await screen.findByRole("heading", { name: "天然橡胶" }),
    ).toBeVisible();
    expect(screen.getByText(/主产区割胶天气/)).toBeVisible();
    expect(screen.queryByText(/在产蛋鸡存栏/)).toBeNull();
    expect(search).toHaveValue("RU");
    await waitFor(() =>
      expect(getProductAnalysis).toHaveBeenCalledWith(
        "RU",
        expect.any(AbortSignal),
      ),
    );
  });

  it("本地服务不可用时仍展示回测并提供重连", async () => {
    vi.mocked(getDashboard).mockRejectedValue(new Error("连接被拒绝"));
    render(<App />);
    expect(await screen.findByText("连接被拒绝")).toBeVisible();
    expect(
      await screen.findByRole("heading", { name: "半年组合收益曲线" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "重新连接" })).toBeEnabled();
  });

  it("重新接续服务已启动的研究", async () => {
    vi.mocked(getDashboard).mockResolvedValue({
      ...dashboard,
      active_run: { run_id: "auto-run-1", status: "running" },
    });
    render(<App />);
    expect(await screen.findByText("Codex 正在更新策略")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "运行全市场研究" }),
    ).toBeDisabled();
    expect(subscribeToRun).toHaveBeenCalledWith(
      expect.objectContaining({
        run_id: "auto-run-1",
        events_url: "/api/runs/auto-run-1/events",
      }),
      expect.any(Function),
      expect.any(Function),
    );
  });

  it("单品种研究传入选中代码并通过轮询收口", async () => {
    window.history.replaceState(null, "", "/#products/RU");
    vi.mocked(createStrategyRun).mockResolvedValue({
      run_id: "manual-run-1",
      status: "queued",
      events_url: "/api/runs/manual-run-1/events",
    });
    vi.mocked(getStrategyRun).mockResolvedValue({
      run_id: "manual-run-1",
      status: "completed",
    });
    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: "运行单品种分析" }),
    );
    expect(createStrategyRun).toHaveBeenCalledWith({
      mode: "single_analysis",
      product: "RU",
      option_analysis_requested: true,
    });
    expect(await screen.findByText("策略已更新")).toBeVisible();
    expect(getStrategyRun).toHaveBeenCalledWith("manual-run-1");
    expect(
      screen.getByRole("button", { name: "运行单品种分析" }),
    ).toBeEnabled();
  });
});
