import { fireEvent, screen, render, cleanup } from "@testing-library/react";
import { expect, test, vi, afterEach } from "vitest";
import { getFrequencyStudy, getFrequencySession } from "../lib/api";
import { FrequencyStudy } from "./FrequencyStudy";
import type { FrequencyResult, FrequencySession } from "../types";
vi.mock("../lib/api", () => ({
  getFrequencyStudy: vi.fn(),
  getFrequencySession: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
test("切换策略后读取该策略采用的版本及逐日判断", async () => {
  const rows: FrequencyResult["rows"] = (["A", "B", "C"] as const).flatMap(
    (group) =>
      (["baseline", "primary", "ranked"] as const).map((variant) => ({
        group,
        variant,
        opened: 1,
        pnl: 10,
        tail_pnl: -1,
        drawdown_pct: 0.2,
        curve: [{ day: "2026-11-20", equity: 200010 }],
      })),
  );
  vi.mocked(getFrequencyStudy).mockResolvedValue({
    status: "complete",
    completed_days: 125,
    rows,
    per_group: { A: "baseline", B: "primary", C: "ranked" },
    term_mode: "compare_only",
    term_pairs: [],
    term: null,
    control: [],
  });
  vi.mocked(getFrequencySession).mockImplementation(
    async (v, g, d) =>
      ({
        day: d,
        cutoff: "2026-11-19T20:00:00+08:00",
        decision: {
          action: "wait",
          product: "",
          legs: [],
          thesis: g + v,
          technical: "趋势",
          fundamentals: "仓单",
          news: "无公告",
          ownership: "未知",
          invalidation: "复核",
        },
        execution: { status: "wait", reason: "等待新公告", exits: [] },
      }) as unknown as FrequencySession,
  );
  render(<FrequencyStudy />);
  expect(await screen.findByText("Bprimary")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("对照策略"), {
    target: { value: "C" },
  });
  expect(await screen.findByText("Cranked")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("决策版本"), {
    target: { value: "baseline" },
  });
  expect(await screen.findByText("Cbaseline")).toBeTruthy();
  expect(
    screen.getByRole("link", { name: /核对该日/ }).getAttribute("href"),
  ).toContain("/2026-11-20");
});
