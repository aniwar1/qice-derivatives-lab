import { useEffect, useState } from "react";
import type { AgentSession, Study } from "../types";
import { FrozenProduct } from "./FrozenProduct";
import { LineChart } from "./LineChart";
import { COLORS, currency, pct, PlanBadge, names, mandates } from "./PlanPresentation";
import { getAgentStudy } from "../lib/api";

type Group = "A" | "B" | "C";
const groups: Group[] = ["A", "B", "C"];
const executions: Record<string, string> = {
  wait: "等待",
  simulated_open: "模拟开仓",
  blocked: "风控拦截",
  unfilled: "未成交",
};
const metric = (value: number | null, formatter: (x: number) => string) =>
  value == null ? "—" : formatter(value);
const filters = {
  all: "全部交易日",
  enter: "入场提议",
  simulated_open: "模拟开仓",
  wait: "主动等待",
  exit: "有平仓",
};
function matches(session: AgentSession, filter: keyof typeof filters) {
  if (filter === "all") return true;
  if (filter === "exit") return session.execution.exits.length > 0;
  if (filter === "enter") return session.decision.action === "enter";
  return session.execution.status === filter;
}

export function AgentStudy({
  namesByProduct,
}: {
  namesByProduct: Record<string, string>;
}) {
  const [study, setStudy] = useState<Study | null>(null);
  const [failure, setFailure] = useState("");
  const [group, setGroup] = useState<Group>("B");
  const [day, setDay] = useState("");
  const [filter, setFilter] = useState<keyof typeof filters>("all");
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      try {
        const data = await getAgentStudy(controller.signal);
        if (controller.signal.aborted) return;
        setStudy(data);
        setFailure("");
        if (data.status === "partial") timer = setTimeout(refresh, 30_000);
      } catch (error) {
        if (!controller.signal.aborted) {
          setFailure(error instanceof Error ? error.message : "读取回测失败");
          timer = setTimeout(refresh, 30_000);
        }
      }
    }
    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, []);
  const report = study?.status === "missing" ? null : study;
  const plan = report?.plans.find((p) => p.id === group);
  const sessions = plan?.sessions.filter((s) => matches(s, filter)) ?? [];
  const session =
    sessions.find((s) => s.day === day) ?? sessions[sessions.length - 1];
  const d = session?.decision;
  return (
    <>
      <header className="page-heading">
        <div>
          <h1>Agent 策略回测</h1>
          <p>
            {report
              ? `${report.start} 至 ${report.end} · ${report.completed_days} / ${report.planned_days} 个交易日已获模型回执 · ${report.model}`
              : "按历史信息截止时点审阅三套策略"}
          </p>
        </div>
        <span className="tag paper-tag">历史模拟</span>
      </header>
      {failure && (
        <p role="alert" className="alert">
          {failure}
        </p>
      )}
      {!study && !failure && <p role="status">正在读取本地回测…</p>}
      {study?.status === "missing" && (
        <div className="surface plan-detail">
          <p>
            本机暂无半年期 Agent
            回测结果。运行历史回放后，可在这里查看逐日判断和收益曲线。
          </p>
          <a href="#baseline">查看半年滚动基线 →</a>
        </div>
      )}
      {report && (
        <>
          {report.status === "partial" && (
            <p className="alert">
              回执尚未齐全。当前权益只计算前 {report.evaluated_days}{" "}
              个连续交易日。
            </p>
          )}
          <div className="strategy-cards">
            {report.plans.map((p) => (
              <article className="surface strategy-card" key={p.id}>
                <div className="card-head">
                  <PlanBadge id={p.id} />
                  <h2>{names[p.id]}</h2>
                </div>
                <div className="plan-metrics">
                  <div>
                    <span>累计收益</span>
                    <strong>{metric(p.stats.return_pct, pct)}</strong>
                  </div>
                  <div>
                    <span>最大回撤</span>
                    <strong>
                      {metric(p.stats.drawdown_pct, (n) => n.toFixed(2) + "%")}
                    </strong>
                  </div>
                </div>
                <p className="plan-thesis">{mandates[p.id]}</p>
                <p className="plan-thesis">
                  开仓 {p.stats.opened} 笔 · 已平 {p.stats.closed} 笔<br />
                  已实现 {metric(p.stats.realized, currency)} 元 · 浮盈亏{" "}
                  {metric(p.stats.open_pnl, currency)} 元
                </p>
                <button
                  type="button"
                  className="text-action"
                  onClick={() => {
                    setGroup(p.id);
                    document
                      .getElementById("study-decisions")
                      ?.scrollIntoView?.({ block: "start" });
                  }}
                >
                  查看逐日决策 →
                </button>
              </article>
            ))}
          </div>
          {!!report.plans[0]?.curve.length && (
            <section className="surface chart-surface">
              <div className="section-heading">
                <h2>模拟组合累计收益</h2>
                <p>逐日收盘估值 · 单位 %</p>
              </div>
              <LineChart
                title="三套 Agent 策略累计收益率"
                format={pct}
                baseline={0}
                labels={["期初", ...report.plans[0].curve.map((p) => p.day)]}
                series={report.plans.map((p) => ({
                  label: p.id + " " + names[p.id],
                  color: COLORS[p.id],
                  values: [0, ...p.curve.map((x) => x.return_pct)],
                }))}
              />
            </section>
          )}
          <section className="surface plan-detail" id="study-decisions">
            <div className="section-heading">
              <h2>逐日决策与执行</h2>
              <div className="study-selects">
                <select
                  aria-label="回放策略"
                  value={group}
                  onChange={(e) => setGroup(e.target.value as Group)}
                >
                  {groups.map((g) => (
                    <option key={g} value={g}>
                      {g} {names[g]}
                    </option>
                  ))}
                </select>
                <select
                  aria-label="交易日筛选"
                  value={filter}
                  onChange={(e) =>
                    setFilter(e.target.value as keyof typeof filters)
                  }
                >
                  {Object.entries(filters).map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
                {session && (
                  <select
                    aria-label="回放日期"
                    value={session.day}
                    onChange={(e) => setDay(e.target.value)}
                  >
                    {sessions.map((s) => (
                      <option key={s.day}>{s.day}</option>
                    ))}
                  </select>
                )}
              </div>
            </div>
            {session && d ? (
              <>
                <p className="muted">
                  信息截止 {session.cutoff.replace("T", " ")} ·{" "}
                  {d.product || "全市场"} ·{" "}
                  {executions[session.execution.status] ??
                    session.execution.status}
                </p>
                <p>{d.thesis}</p>
                <dl className="plan-steps">
                  {[
                    ["技术面", d.technical],
                    ["基本面", d.fundamentals],
                    ["消息面", d.news],
                    ["机构持仓口径", d.ownership],
                    ["失效条件", d.invalidation],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <dt>{label}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
                {d.action === "enter" && (
                  <p>
                    计划风险预算 {(d.risk_bps / 100).toFixed(2)}% · 记账预算{" "}
                    {session.execution.effective_risk_bps == null
                      ? "—"
                      : (session.execution.effective_risk_bps / 100).toFixed(
                          2,
                        ) + "%"}{" "}
                    · 最长持有 {d.holding_days} 个交易日 · {d.legs.join(" / ")}
                    <br />
                    {session.execution.budget_reason && (
                      <>{session.execution.budget_reason} · </>
                    )}
                    {session.execution.reason}
                    <br />
                    报价上限 {d.max_entry_premium || "按预算"} · 手数上限{" "}
                    {d.max_lots || "按预算"} · 收盘止盈位{" "}
                    {d.take_profit_close || "未设"}
                    {d.invalidation_price ? (
                      <> · 标的失效位 {d.invalidation_price}</>
                    ) : null}
                    {d.scenario_low && d.scenario_high ? (
                      <>
                        {" "}
                        · 双尾情景 {d.scenario_low} 至 {d.scenario_high}
                      </>
                    ) : null}
                    {session.execution.raw_open_prices && (
                      <>
                        {" "}
                        · 各腿模拟开盘价{" "}
                        {session.execution.raw_open_prices.join(" / ")}
                      </>
                    )}
                    {session.execution.quantity != null && (
                      <>
                        {" "}
                        · {session.execution.quantity} 手 · 权利金及开仓费用{" "}
                        {currency(session.execution.premium_and_fees ?? 0)} 元
                      </>
                    )}
                  </p>
                )}
                {session.execution.exits.map((x) => (
                  <p key={x.product + x.entry_day}>
                    {x.product} 平仓 · {x.reason} · {currency(x.pnl)} 元
                  </p>
                ))}
                <p className="method-note">
                  引用证据 {d.evidence_ids.join("、") || "未引用"} ·{" "}
                  <a
                    href={`/api/workbench/agent-replay/context/${session.day}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    打开当日冻结的证据与来源
                  </a>
                </p>
              </>
            ) : (
              <p>当前筛选条件下没有已结算的决策。</p>
            )}
          </section>
          {session && (
            <FrozenProduct
              day={session.day}
              candidate={d?.product || ""}
              names={namesByProduct}
            />
          )}
          <details className="surface plan-detail">
            <summary>逐品种证据覆盖</summary>
            <p className="method-note">
              表内为模型上下文中实际包含对应资料的交易日数。期权列按当日入围合约统计。
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>品种</th>
                    <th>技术面</th>
                    <th>期权候选</th>
                    <th>基本面</th>
                    <th>消息面</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(report.context_coverage).map(
                    ([code, row]) => (
                      <tr key={code}>
                        <td>
                          {namesByProduct[code]} {code}
                        </td>
                        <td>{row.technical}</td>
                        <td>{row.option_chain}</td>
                        <td>{row.fundamentals}</td>
                        <td>{row.news}</td>
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>
          </details>
          <details className="surface plan-detail">
            <summary>数据时点、仓位及成交假设</summary>
            <ul>
              {report.assumptions.map((value) => (
                <li key={value}>{value}</li>
              ))}
            </ul>
          </details>
        </>
      )}
    </>
  );
}
