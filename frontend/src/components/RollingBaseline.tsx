import { useState } from "react";
import { getIntradayReference } from "../lib/api";
import { formatTimestamp, humanStatus } from "../lib/model";
import { useVisibleResource } from "../lib/useVisibleResource";
import type {
  RollingBaseline as Report,
  RollingPlan,
  RollingSession,
  StrategyGroup,
} from "../types";
import { FrozenProduct } from "./FrozenProduct";
import { LineChart } from "./LineChart";
import {
  COLORS,
  currency,
  mandates,
  names,
  pct,
  PlanBadge,
} from "./PlanPresentation";

const groups: StrategyGroup[] = ["A", "B", "C"];
const signed = (v: number) => `${v > 0 ? "+" : ""}${currency(v)}`;
const numeric = (v: number | null | undefined, fn = currency) =>
  typeof v === "number" && Number.isFinite(v) ? fn(v) : "—";
const statuses: Record<string, string> = {
  wait: "等待",
  simulated_open: "模拟开仓",
  blocked: "风控拦截",
  unfilled: "未成交",
  missed_preopen: "缺少盘前封存",
  sealed_preopen: "盘前已封存",
  no_decision: "缺少决策",
  historical: "历史回放",
  forward: "前向模拟",
};
const variantNames = { baseline: "原版", primary: "首选", ranked: "排序备选" };
const updaterStates: Record<string, string> = {
  starting: "启动中",
  checking: "核对中",
  ready: "就绪",
  retrying: "等待重试",
  waiting_for_history: "等待历史归档",
};
const statusText = (value?: string) =>
  value ? (statuses[value] ?? humanStatus(value)) : "待核对";

const referenceStates = {
  live: "盘中估值",
  partial: "部分报价可用",
  closed: "休市",
  flat: "各组合空仓",
  unavailable: "暂无可用估值",
};
function Intraday() {
  const { data, error } = useVisibleResource(getIntradayReference, true);
  return (
    <section aria-label="盘中参考" className="surface intraday-strip">
      <div>
        <strong>上次结算持仓 · 盘中参考</strong>
        <span className="muted">
          {data?.base_day ? `持仓截至 ${data.base_day}` : "以最近完整日结为准"}
        </span>
      </div>
      {error ? (
        <p role="status">参考报价读取失败 · {error}</p>
      ) : !data ? (
        <p role="status">正在读取参考报价…</p>
      ) : (
        <>
          <p>
            {referenceStates[data.status]}
            {data.observed_at &&
              ` · ${formatTimestamp(data.observed_at)}`} · {data.note}
          </p>
          {data.status !== "closed" &&
            data.status !== "flat" &&
            data.groups.length > 0 && (
              <ul>
                {data.groups.map((g) => (
                  <li key={g.id}>
                    {g.id} · {numeric(g.pnl_change, signed)} 元{" "}
                    <small>
                      已估 {g.marked_legs}/{g.total_legs} 腿
                    </small>
                  </li>
                ))}
              </ul>
            )}
        </>
      )}
    </section>
  );
}

function Curve({ report }: { report: Report }) {
  const [equity, setEquity] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const dates = Array.from(
    new Set([
      ...report.missing_days,
      ...report.plans.flatMap((p) => [
        ...(p.baseline_point ? [p.baseline_point.day] : []),
        ...p.curve.map((point) => point.day),
      ]),
    ]),
  ).sort();
  const series = report.plans.map((plan) => {
    const values = new Map(
      plan.curve.map((point) => [
        point.day,
        equity ? point.equity : point.return_pct,
      ]),
    );
    if (plan.baseline_point)
      values.set(
        plan.baseline_point.day,
        equity ? plan.baseline_point.equity : plan.baseline_point.return_pct,
      );
    return {
      label: `策略 ${plan.id}`,
      color: COLORS[plan.id],
      values: dates.map((d) => values.get(d) ?? null),
    };
  });
  return (
    <section className="surface chart-section" aria-label="半年组合收益曲线">
      <div className="section-heading">
        <div>
          <h2>半年组合收益曲线</h2>
          <p>基准日收盘归零 · 各组按 20 万元重基</p>
        </div>
        <div className="segmented" aria-label="曲线指标">
          {[false, true].map((v) => (
            <button
              key={String(v)}
              type="button"
              aria-pressed={equity === v}
              onClick={() => setEquity(v)}
            >
              {v ? "重基权益" : "区间收益率"}
            </button>
          ))}
        </div>
      </div>
      <LineChart
        labels={dates}
        series={series}
        baseline={equity ? 200000 : 0}
        format={equity ? currency : pct}
        title="三组半年滚动表现"
      />
      <details
        className="table-details"
        open={expanded}
        onToggle={(e) => setExpanded(e.currentTarget.open)}
      >
        <summary>查看逐日曲线数据</summary>
        {expanded && (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>交易日</th>
                  {series.map((s) => (
                    <th key={s.label}>{s.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {dates.map((d, i) => (
                  <tr key={d}>
                    <th>{d}</th>
                    {series.map((s) => (
                      <td key={s.label}>
                        {numeric(s.values[i], equity ? currency : pct)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </details>
    </section>
  );
}

function DailyRecord({
  session,
  namesByProduct,
}: {
  session: RollingSession;
  namesByProduct: Record<string, string>;
}) {
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const d = session.decision;
  const fills = session.execution;
  const product = d?.product;
  const exits = fills?.exits ?? [];
  const source = session.source === "historical" ? "历史回放" : "前向模拟日结";
  return (
    <>
      <div className="daily-meta">
        <span className="tag">{source}</span>
        <strong>{session.day}</strong>
        <span>
          {session.decision_status
            ? statusText(session.decision_status)
            : statusText(fills?.status)}
        </span>
        {product && (
          <a className="text-action" href={`#products/${product}`}>
            {namesByProduct[product] ?? product} {product} →
          </a>
        )}
      </div>
      {session.cutoff && (
        <p className="muted">信息截止 {formatTimestamp(session.cutoff)}</p>
      )}
      {d?.thesis && <p>{d.thesis}</p>}
      {!d && <p className="empty-note">当日没有可回读的已封存决策。</p>}
      <dl className="plan-steps">
        {[
          ["技术面", d?.technical],
          ["基本面", d?.fundamentals],
          ["消息面", d?.news],
          ["机构持仓口径", d?.ownership],
          ["失效条件", d?.invalidation],
          ["期权腿", d?.legs?.join(" / ")],
          ["执行结果", fills?.reason],
        ]
          .filter(([, v]) => v)
          .map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
      </dl>
      {d?.action === "enter" && (
        <dl className="detail-metrics">
          <div>
            <dt>权利金上限</dt>
            <dd>{numeric(d.max_entry_premium)}</dd>
          </div>
          <div>
            <dt>手数上限</dt>
            <dd>{d.max_lots ?? "—"}</dd>
          </div>
          <div>
            <dt>目标 / 失效价</dt>
            <dd>
              {numeric(d.take_profit_close)} / {numeric(d.invalidation_price)}
            </dd>
          </div>
          <div>
            <dt>执行预算</dt>
            <dd>
              {fills?.effective_risk_bps == null
                ? "—"
                : pct(fills.effective_risk_bps / 100)}
            </dd>
          </div>
          <div>
            <dt>计划风险预算</dt>
            <dd>{d.risk_bps == null ? "—" : pct(d.risk_bps / 100)}</dd>
          </div>
          <div>
            <dt>持有上限</dt>
            <dd>{d.holding_days ?? "—"} 个交易日</dd>
          </div>
          <div>
            <dt>模拟开仓手数</dt>
            <dd>{fills?.quantity ?? "—"}</dd>
          </div>
          <div>
            <dt>权利金及费用</dt>
            <dd>{numeric(fills?.premium_and_fees)} 元</dd>
          </div>
        </dl>
      )}
      {fills?.budget_reason && (
        <p className="method-note">{fills.budget_reason}</p>
      )}
      {exits.length > 0 && (
        <div className="table-scroll">
          <table>
            <caption>当日退出</caption>
            <thead>
              <tr>
                <th>品种</th>
                <th>开仓日</th>
                <th>净盈亏（元）</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {exits.map((x, i) => (
                <tr key={i}>
                  <td>{x.product}</td>
                  <td>{x.entry_day}</td>
                  <td>{numeric(x.pnl, signed)}</td>
                  <td>{x.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {session.source === "historical" && (
        <details
          className="table-details"
          onToggle={(e) => setEvidenceOpen(e.currentTarget.open)}
        >
          <summary>查看当时的原始品种证据</summary>
          {evidenceOpen && (
            <FrozenProduct
              day={session.day}
              candidate={product ?? ""}
              names={namesByProduct}
            />
          )}
        </details>
      )}
      {session.source === "forward" && (
        <div className="method-note">
          {session.recorded_at && (
            <p>日结封存于 {formatTimestamp(session.recorded_at)}</p>
          )}
          {!!session.market_sources?.length && (
            <details>
              <summary>当日行情原件（{session.market_sources.length}）</summary>
              <ul>
                {session.market_sources.map((s, i) => (
                  <li key={i}>
                    {s.https ? (
                      <a href={s.https} target="_blank" rel="noreferrer">
                        {s.exchange} · {s.type === "option" ? "期权" : "期货"} ·{" "}
                        {s.day}
                      </a>
                    ) : (
                      s.exchange
                    )}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {!!session.market_gaps?.length && (
            <p>行情源有 {session.market_gaps.length} 项缺口。</p>
          )}
        </div>
      )}
    </>
  );
}

function PlanDetail({
  plan,
  requestedDay,
  onSelect,
  namesByProduct,
}: {
  plan: RollingPlan;
  requestedDay?: string;
  onSelect: (id: StrategyGroup, day?: string) => void;
  namesByProduct: Record<string, string>;
}) {
  const active = requestedDay ?? plan.sessions.at(-1)?.day;
  const session = plan.sessions.find((s) => s.day === active);
  return (
    <section
      className="surface plan-detail"
      id="rolling-plan"
      tabIndex={-1}
      aria-label="策略计划与逐日记录"
    >
      <div className="section-heading">
        <div>
          <h2>
            策略 {plan.id} · {names[plan.id]}
          </h2>
          <p>
            {variantNames[plan.variant] ?? plan.variant} · 策略计划与逐日记录
          </p>
        </div>
        <label>
          交易日{" "}
          <select
            aria-label="选择计划交易日"
            value={session?.day ?? ""}
            onChange={(e) => onSelect(plan.id, e.target.value)}
          >
            {!session && <option value="">选择交易日</option>}
            {[...plan.sessions].reverse().map((s) => (
              <option key={s.day}>{s.day}</option>
            ))}
          </select>
        </label>
      </div>
      <p>{mandates[plan.id]}</p>
      {plan.positions && (
        <details className="table-details">
          <summary>
            查看 {plan.latest_day} 日终持仓（{plan.positions.length} 笔）
          </summary>
          {plan.positions.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>品种</th>
                    <th>开仓日</th>
                    <th>期权腿</th>
                    <th>手数</th>
                    <th>入场成本（元）</th>
                    <th>归档估值（元）</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.positions.map((p, i) => (
                    <tr key={i}>
                      <td>
                        <a href={`#products/${p.product}`}>
                          {namesByProduct[p.product] ?? p.product}
                        </a>
                      </td>
                      <td>{p.entry_day}</td>
                      <td>{p.legs.join(" / ")}</td>
                      <td>{p.quantity}</td>
                      <td>{numeric(p.cost)}</td>
                      <td>{numeric(p.marked_value)}</td>
                      <td>
                        {p.pending_exit == null
                          ? "退出状态待核对"
                          : p.pending_exit
                            ? "待退出"
                            : "持有"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p>该策略日终空仓。</p>
          )}
        </details>
      )}
      {!!plan.curve.find((p) => p.day === active)?.stale_marks.length && (
        <p className="alert">当日部分持仓缺少新估值，沿用此前收盘价。</p>
      )}
      {session ? (
        <DailyRecord
          key={session.day}
          session={session}
          namesByProduct={namesByProduct}
        />
      ) : (
        <p className="empty-note">
          {requestedDay
            ? `${requestedDay} 没有该策略的窗口内记录。`
            : "当前窗口暂无该策略的逐日记录。"}
        </p>
      )}
    </section>
  );
}

export function RollingWorkbench({
  report,
  error,
  busy,
  refresh,
  selected,
  day,
  onSelect,
  namesByProduct,
}: {
  report: Report | null;
  error: string;
  busy: boolean;
  refresh: () => void;
  selected?: StrategyGroup;
  day?: string;
  onSelect: (group: StrategyGroup, day?: string) => void;
  namesByProduct: Record<string, string>;
}) {
  const plan = report?.plans.find((p) => p.id === (selected ?? "A"));
  const updater = report?.updater;
  const updateReason =
    updater?.reason ||
    (updater?.worker_status === "calendar_exhausted"
      ? "没有下一交易日的已核实日历。"
      : null);
  const hasData =
    !!report && report.status !== "missing" && !!report.plans.length;
  return (
    <>
      <header className="page-heading">
        <div>
          <h1>半年滚动基线</h1>
          <p>
            {report?.start && report.end
              ? `${report.start} 至 ${report.end} · 三组模拟账户 · 北京时间`
              : "三套策略的区间收益与模拟日结"}
          </p>
        </div>
        <button
          className="primary-action"
          type="button"
          onClick={refresh}
          disabled={busy}
        >
          {busy ? "正在更新…" : "刷新基线"}
        </button>
      </header>
      {error && (
        <p role="alert" className="alert">
          {report ? "刷新失败，保留上次数据" : "基线读取失败"} · {error}
        </p>
      )}
      {report && (
        <div className="baseline-status">
          <strong>
            {{
              current: "已覆盖应结交易日",
              behind: "日结待补",
              calendar_unknown: "交易日历待核实",
              missing: "尚无基线数据",
            }[report.freshness] ?? "资料待核实"}
          </strong>
          <span>已结至 {report.latest_day ?? "—"}</span>
          <span>应结至 {report.latest_expected_day ?? "—"}</span>
          <span>每 60 秒刷新</span>
          {report.updater && (
            <span>
              后台更新器 · {updaterStates[report.updater.state] ?? "状态待核对"}
            </span>
          )}
          {report.as_of && (
            <span>视图更新 {formatTimestamp(report.as_of)}</span>
          )}
          {report.latest_recorded_at && (
            <span>
              最近日结封存 {formatTimestamp(report.latest_recorded_at)}
            </span>
          )}
        </div>
      )}
      {updateReason && (
        <p className="alert" role="status">
          后台更新 · {updateReason}
          {updater?.next_session && (
            <span> · 对应交易日 {updater.next_session}</span>
          )}
          {updater?.last_settled && (
            <span> · 上次结算 {updater.last_settled}</span>
          )}
        </p>
      )}
      {!!report?.missing_days.length && (
        <details className="alert">
          <summary>
            缺少 {report.missing_days.length} 个交易日的日结 · 查看日期
          </summary>
          <p>{report.missing_days.join("、")}</p>
        </details>
      )}
      {report && report.status === "partial" && !report.missing_days.length && (
        <p className="alert">当前窗口只有部分已核实的日结数据。</p>
      )}
      {!!report?.issues?.length && (
        <details className="alert">
          <summary>核对提示（{report.issues.length}）</summary>
          <ul>
            {report.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </details>
      )}
      {report && report.status === "missing" && (
        <p className="empty-note">本机尚无可用于半年滚动基线的日结记录。</p>
      )}
      {hasData && <Curve report={report} />}
      <div className="strategy-cards">
        {groups.map((id) => {
          const stats = report?.plans.find((p) => p.id === id)?.stats;
          return (
            <article
              className={`surface strategy-card rolling-card ${(selected ?? "A") === id ? "selected" : ""}`}
              key={id}
              style={{ borderTopColor: COLORS[id] }}
            >
              <div className="card-head">
                <PlanBadge id={id} />
                <h2>{names[id]}</h2>
              </div>
              <div className="plan-metrics">
                <div>
                  <span>区间收益</span>
                  <strong>{numeric(stats?.return_pct, pct)}</strong>
                </div>
                <div>
                  <span>最大回撤</span>
                  <strong>
                    {numeric(stats?.drawdown_pct, (v) => `${v.toFixed(2)}%`)}
                  </strong>
                </div>
              </div>
              {stats && (
                <p className="rolling-gain">
                  重基净盈亏{" "}
                  <strong>{numeric(stats.net_pnl, signed)} 元</strong>
                </p>
              )}
              <p className="plan-thesis">
                {stats
                  ? `开仓 ${stats.opened} · 平仓 ${stats.closed} · 持仓 ${stats.open_positions ?? "—"}`
                  : mandates[id]}
              </p>
              {stats && (
                <button
                  className="text-action"
                  type="button"
                  onClick={() => onSelect(id)}
                >
                  查看策略 {id} 计划 →
                </button>
              )}
            </article>
          );
        })}
      </div>
      <Intraday />
      {plan && (
        <PlanDetail
          plan={plan}
          requestedDay={day}
          onSelect={onSelect}
          namesByProduct={namesByProduct}
        />
      )}
      {hasData && (
        <details className="surface plan-detail">
          <summary>计算口径与历史区间</summary>
          <p>
            六个自然月前同日收盘为起点，非交易日采用此前可核实的基准日收盘。盘中参考估值单列，正式曲线采用已结交易日。
          </p>
          <ul>
            {report.plans.map((p) => (
              <li key={p.id}>
                策略 {p.id} · 基准日 {p.baseline_day ?? "—"} · 原账户基准权益{" "}
                {numeric(p.baseline_equity)} 元 · 最近原账户权益{" "}
                {numeric(p.stats.raw_equity)} 元 · 区间原账户盈亏{" "}
                {numeric(p.stats.raw_net_pnl, signed)} 元
              </li>
            ))}
          </ul>
          {report.historical_end && (
            <p>历史回放截至 {report.historical_end}，此后接续前向模拟日结。</p>
          )}
          <ul>
            {report.assumptions?.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}
