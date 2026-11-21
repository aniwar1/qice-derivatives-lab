import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { createStrategyRun, getDashboard, getProductAnalysis, getStrategyRun, getWorkbenchBacktest, subscribeToRun } from "./lib/api";
import type { StrategyPlan, WorkbenchBacktest } from "./types";

vi.mock("./lib/api", () => ({
  getDashboard: vi.fn(),
  getAgentStudy: vi.fn().mockResolvedValue({ status: "missing", plans: [] }),
  getResearchRuns: vi.fn().mockResolvedValue([]),
  getWorkbenchBacktest: vi.fn(),
  getProductAnalysis: vi.fn(),
  getStrategyRun: vi.fn(),
  createStrategyRun: vi.fn(),
  subscribeToRun: vi.fn(),
}));

const day = "2026-10-30";
const planFixture = (id: StrategyPlan["id"], name: string): StrategyPlan => ({
  id, name, style: "模拟组合", thesis: "研究假设", steps: [["候选排序", "前一交易日冻结证据"]], limit: "样本限制",
  stats: { opened: 1, closed: 0, open_positions: 1, equity: 101000, return_pct: 1, drawdown_pct: 2, realized: 0, open_pnl: 1000 },
  curve: [{ day, equity: 101000, return_pct: 1, realized: 0, open_pnl: 1000, open_positions: 1 }],
  sessions: [{ day, information_day: "2026-10-29", exits: [], outcome: {
    status: "opened", reason: null, product: "JD", contract: "JD2701", quantity: 1,
    paid: 300, fee: 3, legs: [{ symbol: "JD2701-C-4000", open_price: 30 }],
  } }],
});
const backtest: WorkbenchBacktest = {
  status: "ready", batch: "2026-10", days: [day], initial_cash: 100000, daily_cap: 10000, fee_per_side: 3,
  covered_products: ["JD", "RU"], configured_count: 2, market_sha256: "",
  plans: [planFixture("A", "多因子方向买权"), planFixture("B", "技术择强买权"), planFixture("C", "事件驱动宽跨式")],
};
const dashboard = {
  product_catalog: [{ symbol: "JD", name: "鸡蛋", exchange: "DCE" }, { symbol: "RU", name: "天然橡胶", exchange: "SHFE" }],
  system: { status: "healthy" },
};

describe("期策工作台", () => {
  afterEach(cleanup);
  beforeEach(() => {
    window.history.replaceState(null, "", "/#overview");
    window.scrollTo = vi.fn();
    vi.resetAllMocks();
    vi.mocked(getDashboard).mockResolvedValue(dashboard);
    vi.mocked(getWorkbenchBacktest).mockResolvedValue(backtest);
    vi.mocked(getProductAnalysis).mockImplementation(async (product) => ({
      product, observation: null, evidence_run_id: null, analysis: null,
    }));
    vi.mocked(getStrategyRun).mockResolvedValue({ run_id: "test-run", status: "running" });
    vi.mocked(subscribeToRun).mockReturnValue(vi.fn());
  });

  it("并列展示三套计划并从曲线下钻到期权腿", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: "多因子方向买权" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "技术择强买权" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "事件驱动宽跨式" })).toBeVisible();
    const chart = within(screen.getByRole("region", { name: "组合收益曲线" }));
    fireEvent.click(chart.getByRole("button", { name: "权益" }));
    expect(chart.getByRole("button", { name: "权益" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getAllByRole("button", { name: "查看详细计划" })[2]);
    expect(await screen.findByRole("heading", { name: "策略计划" })).toBeVisible();
    expect(screen.getByText("JD2701-C-4000")).toBeVisible();
    expect(screen.getByText(/信息截止 2026-10-29/)).toBeVisible();
  });

  it("品种搜索切换专属观察项且保留搜索词", async () => {
    window.history.replaceState(null, "", "/#products/JD");
    render(<App />);
    expect(await screen.findByRole("heading", { name: "鸡蛋" })).toBeVisible();
    expect(screen.getByText(/在产蛋鸡存栏/)).toBeVisible();
    const search = screen.getByRole("textbox", { name: /品种/ });
    fireEvent.change(search, { target: { value: "RU" } });
    fireEvent.click(screen.getByRole("button", { name: /天然橡胶/ }));
    expect(await screen.findByRole("heading", { name: "天然橡胶" })).toBeVisible();
    expect(screen.getByText(/主产区割胶天气/)).toBeVisible();
    expect(screen.queryByText(/在产蛋鸡存栏/)).toBeNull();
    expect(search).toHaveValue("RU");
    await waitFor(() => expect(getProductAnalysis).toHaveBeenCalledWith("RU", expect.any(AbortSignal)));
  });

  it("本地服务不可用时仍展示回测并提供重连", async () => {
    vi.mocked(getDashboard).mockRejectedValue(new Error("连接被拒绝"));
    render(<App />);
    expect(await screen.findByText("连接被拒绝")).toBeVisible();
    expect(await screen.findByRole("heading", { name: "组合收益曲线" })).toBeVisible();
    expect(screen.getByRole("button", { name: "重新连接" })).toBeEnabled();
  });

  it("重新接续服务已启动的研究", async () => {
    vi.mocked(getDashboard).mockResolvedValue({ ...dashboard, active_run: { run_id: "auto-run-1", status: "running" } });
    render(<App />);
    expect(await screen.findByText("Codex 正在更新策略")).toBeVisible();
    expect(screen.getByRole("button", { name: "运行全市场研究" })).toBeDisabled();
    expect(subscribeToRun).toHaveBeenCalledWith(
      expect.objectContaining({ run_id: "auto-run-1", events_url: "/api/runs/auto-run-1/events" }),
      expect.any(Function), expect.any(Function),
    );
  });

  it("单品种研究传入选中代码并通过轮询收口", async () => {
    window.history.replaceState(null, "", "/#products/RU");
    vi.mocked(createStrategyRun).mockResolvedValue({ run_id: "manual-run-1", status: "queued", events_url: "/api/runs/manual-run-1/events" });
    vi.mocked(getStrategyRun).mockResolvedValue({ run_id: "manual-run-1", status: "completed" });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "运行单品种分析" }));
    expect(createStrategyRun).toHaveBeenCalledWith({ mode: "single_analysis", product: "RU", option_analysis_requested: true });
    expect(await screen.findByText("策略已更新")).toBeVisible();
    expect(getStrategyRun).toHaveBeenCalledWith("manual-run-1");
    expect(screen.getByRole("button", { name: "运行单品种分析" })).toBeEnabled();
  });
});
