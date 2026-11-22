import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getAgentStudy, getFrozenContext } from "../lib/api";
import type { AgentSession, FrozenContext, Study } from "../types";
import { AgentStudy } from "./AgentStudy";

vi.mock("../lib/api", () => ({
  getAgentStudy: vi.fn(),
  getFrozenContext: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
const namesByProduct = { CU: "铜", V: "PVC" };
const days = ["2026-05-21", "2026-05-22"];
const session = (day: string, plan: string): AgentSession => ({
  day,
  cutoff: "2026-05-20T20:00:00+08:00",
  context_sha256: "",
  decision: {
    product: "CU",
    action: "wait",
    technical: "缺少日线",
    fundamentals: "仓单下降",
    news: "暂无新公告",
    ownership: "受益归属不明",
    thesis: plan + day,
    invalidation: "重新评估库存",
    risk_bps: 0,
    holding_days: 0,
    legs: [],
    evidence_ids: [],
  },
  execution: { status: "wait", reason: "流动性不足", exits: [] },
});
const study: Study = {
  status: "partial",
  start: days[0],
  end: days[1],
  model: "test",
  completed_days: 2,
  planned_days: 3,
  evaluated_days: 2,
  context_coverage: {},
  assumptions: [],
  plans: (["A", "B", "C"] as const).map((id) => ({
    id,
    curve: [],
    sessions: days.map((d) => session(d, id)),
    stats: {
      realized: 0,
      open_pnl: 0,
      opened: 0,
      closed: 0,
      drawdown_pct: 0,
      return_pct: 0,
    },
  })),
};
const frozen = (day: string): FrozenContext => ({
  trade_date: day,
  daily_data_date: "2026-05-20",
  ownership: "自营方向未知",
  products: Object.fromEntries(
    ["CU", "V"].map((p) => [
      p,
      { technical: null, options: [], monthly_aggregate: {} },
    ]),
  ),
  documents: [
    {
      id: "S1",
      products: ["CU"],
      dimension: "fundamentals",
      scope: "direct",
      title: "铜仓单",
      excerpt: "仓单样本 " + day,
      publisher: "上期所",
      available_at: days[0],
      url: "https://www.shfe.com.cn/",
    },
  ],
});
describe("半年 Agent 回测", () => {
  it("新的本地检出显示导入入口和基线链接", async () => {
    vi.mocked(getAgentStudy).mockResolvedValue({
      status: "missing",
      plans: [],
    });
    render(<AgentStudy namesByProduct={namesByProduct} />);
    expect(
      await screen.findByRole("link", { name: /半年滚动基线/ }),
    ).toHaveAttribute("href", "#baseline");
    expect(getFrozenContext).not.toHaveBeenCalled();
  });
  it("按策略和日期检查决策，并按所选品种展示同期证据", async () => {
    vi.mocked(getAgentStudy).mockResolvedValue(study);
    vi.mocked(getFrozenContext).mockImplementation(async (day) => frozen(day));
    render(<AgentStudy namesByProduct={namesByProduct} />);
    expect(await screen.findByText(/回执尚未齐全/)).toBeVisible();
    expect(await screen.findByText("仓单样本 " + days[1])).toBeVisible();
    fireEvent.change(screen.getByLabelText("回放日期"), {
      target: { value: days[0] },
    });
    expect(await screen.findByText("仓单样本 " + days[0])).toBeVisible();
    fireEvent.change(screen.getByLabelText("回放策略"), {
      target: { value: "C" },
    });
    expect(screen.getByText("C" + days[0])).toBeVisible();
    fireEvent.change(screen.getByLabelText("历史证据品种"), {
      target: { value: "V" },
    });
    expect(screen.queryByText("仓单样本 " + days[0])).toBeNull();
    expect(screen.getByText("自营方向未知")).toBeVisible();
  });
});
