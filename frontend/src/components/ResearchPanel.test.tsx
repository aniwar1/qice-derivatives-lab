import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { getResearchDetail, getResearchReplay, getResearchRuns } from "../lib/api";
import { ResearchPanel } from "./ResearchPanel";

vi.mock("../lib/api", () => ({
  getResearchRuns: vi.fn(), getResearchDetail: vi.fn(), getResearchReplay: vi.fn(),
}));

it("按北京时间重放历史记录，并隔离截止后证据", async () => {
  vi.mocked(getResearchRuns).mockResolvedValue([
    { run_id: "run-1", status: "completed", mode: "single_analysis", product: "JD", decision: "wait", created_at: "2026-09-11T15:00:00+08:00" },
  ]);
  vi.mocked(getResearchDetail).mockResolvedValue({
    run: { run_id: "run-1", status: "completed" }, archive: null, archive_sha256: null,
    candidate_status: "not_requested", excluded_count: 0, opportunities: [], comparison: null,
  });
  vi.mocked(getResearchReplay).mockResolvedValue({
    review_as_of: "2026-09-11T14:00:00+08:00", evidence_as_of: "2026-09-11T14:00:00+08:00",
    validation: "not_published", excluded_after_cutoff: 1, observations: [], followup: [],
    followup_truncated: false, invalidation_observed: null,
  });
  render(<ResearchPanel revision={0} />);
  const moment = await screen.findByLabelText("回放时点（北京时间）");
  fireEvent.change(moment, { target: { value: "2026-09-11T14:00" } });
  fireEvent.click(screen.getByRole("button", { name: "查看回放" }));
  expect(await screen.findByText(/策略在所选时点尚未发布/)).toHaveTextContent("行情 1 条");
  expect(getResearchReplay).toHaveBeenCalledWith("run-1", "2026-09-11T14:00:00+08:00", expect.any(AbortSignal));
  fireEvent.click(screen.getByRole("button", { name: "查看回放" }));
  await waitFor(() => expect(getResearchReplay).toHaveBeenCalledTimes(2));
});
