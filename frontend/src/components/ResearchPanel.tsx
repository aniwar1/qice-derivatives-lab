import { useEffect, useState } from "react";
import { getResearchDetail, getResearchReplay, getResearchRuns } from "../lib/api";
import { humanStatus } from "../lib/model";
import type { ResearchDetail, ResearchObservation, ResearchReplay, ResearchRun } from "../types";

const timeFormatter = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "medium", timeStyle: "medium", timeZone: "Asia/Shanghai",
});

const validationLabels: Record<string, string> = {
  passed: "通过当时的策略校验",
  failed: "策略未通过回放校验",
  no_strategy: "本轮没有可回放的策略",
  not_published: "策略在所选时点尚未发布",
  archive_missing: "本轮未保存行情档案",
  policy_changed: "规则已变化，保留原结论供查阅",
  evidence_missing: "所选时点缺少可用原始行情",
};

function ObservationList({ observations, runId }: { observations: ResearchObservation[]; runId: string }) {
  return (
    <ul className="research-observations">
      {observations.map((item) => (
        <li key={`${item.contract}-${item.observed_at}`}>
          <strong>{item.contract} · {item.price}</strong>
          <span>行情 {timeFormatter.format(new Date(item.observed_at))}</span>
          <span>采集 {timeFormatter.format(new Date(item.available_at))}</span>
          <a href={item.source} target="_blank" rel="noreferrer">来源</a>
          <details>
            <summary>证据摘要和日线切片</summary>
            <code>{item.artifact_sha256}</code>{" "}
            <a href={`/api/research/runs/${encodeURIComponent(runId)}/evidence/${item.artifact_sha256}`} download>下载行情原件</a>
            {item.daily_bars.length > 0 && (
              <div className="research-table-wrap">
                <table>
                  <caption>采集时可见日线 · {item.daily_status === "verified" ? "已验证" : "研究参考"}</caption>
                  <thead><tr><th>交易日</th><th>开</th><th>高</th><th>低</th><th>收</th></tr></thead>
                  <tbody>{item.daily_bars.map((bar) => (
                    <tr key={bar.date}><td>{bar.date}</td><td>{bar.open ?? "—"}</td><td>{bar.high ?? "—"}</td><td>{bar.low ?? "—"}</td><td>{bar.close ?? "—"}</td></tr>
                  ))}</tbody>
                </table>
              </div>
            )}
          </details>
        </li>
      ))}
    </ul>
  );
}

export function ResearchPanel({ revision, initialRunId }: { revision: number; initialRunId?: string }) {
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [selected, setSelected] = useState(initialRunId ?? "");
  const [detail, setDetail] = useState<ResearchDetail | null>(null);
  const [replay, setReplay] = useState<ResearchReplay | null>(null);
  const [cutoff, setCutoff] = useState("");
  const [replayRequest, setReplayRequest] = useState<{ runId: string; at: string } | null>(null);
  const [error, setError] = useState("");
  const [replayError, setReplayError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void getResearchRuns(controller.signal).then((items) => {
      if (controller.signal.aborted) return;
      setRuns(items);
      setSelected((current) => current || (items[0]?.run_id ?? ""));
    }).catch(() => { if (!controller.signal.aborted) setError("研究记录读取失败。"); });
    return () => controller.abort();
  }, [revision]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setError("");
    void getResearchDetail(selected, controller.signal).then((value) => {
      if (!controller.signal.aborted) setDetail(value);
    }).catch((reason: Error) => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [selected, revision]);

  useEffect(() => {
    if (!replayRequest) return;
    const controller = new AbortController();
    setReplayError("");
    void getResearchReplay(replayRequest.runId, replayRequest.at, controller.signal).then((value) => {
      if (!controller.signal.aborted) setReplay(value);
    }).catch((reason: Error) => { if (!controller.signal.aborted) setReplayError(reason.message); });
    return () => controller.abort();
  }, [replayRequest]);

  const current = detail?.run.run_id === selected ? detail : null;
  const choose = (runId: string) => {
    setSelected(runId);
    setDetail(null);
    setReplay(null);
    setReplayRequest(null);
    setCutoff("");
  };

  return (
    <section className="research-panel" aria-labelledby="research-title">
      <h2 id="research-title">研究档案与回放</h2>
      {error && <p role="alert">{error}</p>}
      {!runs.length && !error && <p>完成一次研究后，可在这里查看证据与机会。</p>}
      {!!runs.length && (
        <label className="research-control">研究记录
          <select value={selected} onChange={(event) => choose(event.target.value)}>
            {selected && !runs.some((item) => item.run_id === selected) && <option value={selected}>指定研究记录</option>}
            {runs.map((item) => (
              <option key={item.run_id} value={item.run_id}>
                {timeFormatter.format(new Date(item.created_at))} · {item.product ?? "全市场"} · {humanStatus(item.status)}
              </option>
            ))}
          </select>
        </label>
      )}
      {current && (
        <>
          {current.run.result && (
            <details className="research-section" open>
              <summary>本轮策略与反证</summary>
              <p>{current.run.result.strategy.rationale.thesis}</p>
              <p>最强反证 · {current.run.result.strategy.rationale.strongest_counterevidence}</p>
              {!!current.run.result.strategy.evidence_gaps.length && (
                <p>证据缺口 · {current.run.result.strategy.evidence_gaps.join("、")}</p>
              )}
            </details>
          )}
          {current.archive ? (
            <details className="research-section">
              <summary>原始取证 · {current.archive.observations.length}/{current.archive.expected_products.length} 个品种有行情</summary>
              <p>取证完成于 {timeFormatter.format(new Date(current.archive.captured_at))}（北京时间）。</p>
              <ObservationList observations={current.archive.observations} runId={selected} />
              {!!Object.keys(current.archive.gaps).length && (
                <ul>{Object.entries(current.archive.gaps).map(([symbol, gap]) => <li key={symbol}>{symbol} · {gap}</li>)}</ul>
              )}
            </details>
          ) : <p>这条记录没有已归档的行情证据。</p>}

          <div className="research-section">
            <h3>通过校验的买方机会</h3>
            {current.candidate_status === "not_returned" && <p>本轮模型没有返回补充候选清单。</p>}
            {current.candidate_status === "invalid" && <p>补充候选清单未通过格式校验。</p>}
            {current.excluded_count > 0 && <p>{current.excluded_count} 条候选未通过资格或证据检查。</p>}
            {current.opportunities.length === 0 && <p>没有可展示的合格机会。</p>}
            {current.comparison && (
              <p>相比上一轮同范围研究，新增 {current.comparison.added.join("、") || "无"}，
                移出 {current.comparison.removed.join("、") || "无"}，条件变化 {current.comparison.changed.join("、") || "无"}。</p>
            )}
            <div className="research-opportunities">
              {current.opportunities.map((item) => (
                <article key={item.product}>
                  <h4>{item.product_name ?? item.product} · {item.option_symbol}</h4>
                  <p>{item.decision === "long_call" ? "买方看涨" : "买方看跌"} · {item.thesis}</p>
                  <p>{item.why_now}</p>
                  <dl>
                    <div><dt>触发</dt><dd>{item.trigger ?? "—"}</dd></div>
                    <div><dt>失效</dt><dd>{item.invalidation ?? "—"}</dd></div>
                    <div><dt>目标</dt><dd>{item.target ?? "—"}</dd></div>
                    <div><dt>最迟复核</dt><dd>{timeFormatter.format(new Date(item.reassess_by))}</dd></div>
                  </dl>
                  <p>最强反证 · {item.counterevidence}</p>
                  {!!item.evidence_gaps.length && <p>待核验 · {item.evidence_gaps.join("、")}</p>}
                  <ul>{item.option_sources.map((source, index) => <li key={`${source.url}-${index}`}><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a></li>)}</ul>
                </article>
              ))}
            </div>
          </div>

          <div className="research-section">
            <h3>按时点回放</h3>
            <form className="research-controls" onSubmit={(event) => {
              event.preventDefault();
              setReplay(null);
              setReplayError("");
              setReplayRequest({ runId: selected, at: cutoff ? `${cutoff}:00+08:00` : "" });
            }}>
              <label className="research-control">回放时点（北京时间）
                <input type="datetime-local" value={cutoff} onChange={(event) => setCutoff(event.target.value)} />
              </label>
              <button className="primary-action" type="submit">查看回放</button>
            </form>
            {replayRequest && !replayError && !replay && <p role="status">正在回放…</p>}
            {replayError && <p role="alert">{replayError}</p>}
            {replay && (
              <div aria-live="polite">
                <p>{validationLabels[replay.validation] ?? replay.validation}。证据截止于 {timeFormatter.format(new Date(replay.evidence_as_of))}，排除截止之后才可用的行情 {replay.excluded_after_cutoff} 条。</p>
                <ObservationList observations={replay.observations} runId={selected} />
                <details open={!!replay.invalidation_observed}>
                  <summary>同合约后续行情 · {replay.followup.length} 条归档样本{replay.followup_truncated ? "（已截取）" : ""}</summary>
                  {replay.invalidation_observed ? (
                    <p>归档样本观察到失效价触及，价格 {replay.invalidation_observed.price}，时间 {timeFormatter.format(new Date(replay.invalidation_observed.observed_at))}。</p>
                  ) : <p>{replay.followup.length ? "现有样本未观察到失效价触及，样本间行情未知。" : "当前时点没有可用的同合约后续样本。"}</p>}
                </details>
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
