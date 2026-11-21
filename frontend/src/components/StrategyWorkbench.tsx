import { useState } from "react";
import type { BacktestSession, StrategyPlan, WorkbenchBacktest } from "../types";
import { LineChart } from "./LineChart";

export const COLORS = { A: "#235fb3", B: "#087d66", C: "#a65b23" };
export const currency = (value: number) => new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
export const pct = (value: number) => `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
const shortCurrency = (value: number) => `${(value / 10000).toFixed(1)} 万`;
const signed = (value: number) => `${value > 0 ? "+" : ""}${currency(value)}`;
const tone = (value: number) => value < 0 ? "negative" : value > 0 ? "positive" : "";

export function PlanBadge({ id }: { id: StrategyPlan["id"] }) {
  return <span className={`plan-badge plan-${id.toLowerCase()}`}>{id}</span>;
}

function ComparisonChart({ data }: { data: Extract<WorkbenchBacktest, { status: "ready" }> }) {
  const [metric, setMetric] = useState<"return_pct" | "equity">("return_pct");
  const [checked, setChecked] = useState(() => new Set(["A", "B", "C"]));
  const format = metric === "return_pct" ? pct : shortCurrency;
  const series = data.plans.filter((plan) => checked.has(plan.id)).map((plan) => ({
    label: `${plan.id} ${plan.name}`, color: COLORS[plan.id],
    values: [metric === "return_pct" ? 0 : data.initial_cash, ...(plan.curve ?? []).map((point) => point[metric])],
  }));
  return (
    <section className="surface chart-surface" aria-labelledby="equity-title">
      <div className="section-heading">
        <div><h2 id="equity-title">组合收益曲线</h2><p>逐交易日收盘估值 · 含已实现和持仓浮盈亏</p></div>
        <div className="segmented" aria-label="曲线口径">
          <button type="button" aria-pressed={metric === "return_pct"} onClick={() => setMetric("return_pct")}>收益率</button>
          <button type="button" aria-pressed={metric === "equity"} onClick={() => setMetric("equity")}>权益</button>
        </div>
      </div>
      <div className="chart-legend" aria-label="选择展示组合">
        {data.plans.map((plan) => (
          <label key={plan.id}><input type="checkbox" checked={checked.has(plan.id)} onChange={() => setChecked((old) => {
            const next = new Set(old);
            if (next.has(plan.id)) next.delete(plan.id); else next.add(plan.id);
            return next;
          })} /><i style={{ background: COLORS[plan.id] }} />{plan.id} {plan.name}</label>
        ))}
      </div>
      <LineChart labels={["期初", ...data.days]} series={series} format={format} baseline={metric === "return_pct" ? 0 : data.initial_cash} title="三组历史权益曲线" />
      <details className="table-details">
        <summary>查看逐日曲线数据</summary>
        <div className="table-scroll">
          <table><thead><tr><th>交易日</th>{data.plans.map((plan) => <th key={plan.id}>{plan.id} {plan.name}</th>)}</tr></thead>
            <tbody>{data.days.map((day, index) => <tr key={day}><td>{day}</td>{data.plans.map((plan) => (
              <td key={plan.id}>{metric === "equity" ? currency(plan.curve![index].equity) : pct(plan.curve![index].return_pct)}</td>
            ))}</tr>)}</tbody>
          </table>
        </div>
      </details>
    </section>
  );
}

export function StrategyOverview({ data, error, openPlan }: { data: WorkbenchBacktest | null; error: string; openPlan: (id: StrategyPlan["id"]) => void }) {
  return (
    <>
      <header className="page-heading">
        <div><h1>策略总览</h1><p>{data?.status === "ready" ? `${data.days[0]} 至 ${data.days[data.days.length - 1]} · ${data.days.length} 个交易日 · ${data.covered_products.length} / ${data.configured_count} 个品种覆盖期权链` : "商品期货与期权研究"}</p></div>
        <span className="tag paper-tag">历史模拟</span>
      </header>
      {error && <p className="alert" role="alert">{error}</p>}
      {!data && !error && <p role="status">正在读取回测数据…</p>}
      {data?.status === "missing" && <p className="alert">尚未导入本地 A/B/C 回测资料。可先查看策略计划。</p>}
      {data && <div className="strategy-cards">{data.plans.map((plan) => (
        <article className="surface strategy-card" key={plan.id}>
          <div className="card-head"><PlanBadge id={plan.id} /><div><h2>{plan.name}</h2><p>{plan.style}</p></div></div>
          {plan.stats && <div className="plan-metrics">
            <div><span>期末收益率</span><strong className={tone(plan.stats.return_pct)}>{pct(plan.stats.return_pct)}</strong></div>
            <div><span>最大回撤</span><strong>{plan.stats.drawdown_pct.toFixed(2)}%</strong></div>
          </div>}
          <p className="plan-thesis">{plan.thesis}</p>
          <button className="text-action" type="button" onClick={() => openPlan(plan.id)}>查看详细计划 <span aria-hidden="true">→</span></button>
        </article>
      ))}</div>}
      {data?.status === "ready" && <ComparisonChart data={data} />}
      {data?.status === "ready" && <p className="method-note">以公开开盘价模拟成交。各组初始资金 ¥{currency(data.initial_cash)}，单日新仓上限 ¥{currency(data.daily_cap)}，费用 ¥{data.fee_per_side}/手/边。期末持仓按收盘市值估值，未来平仓费未计入。</p>}
    </>
  );
}

const exitReasons: Record<string, string> = {
  adaptive_premium_stop: "权利金动态止损", adaptive_trailing_profit_exit: "移动止盈",
  underlying_structure_invalidated: "标的结构失效", post_event_premium_weakness: "事件后权利金转弱",
};
const entries: Record<string, string> = {
  A_forced_original_gates_unverified: "强制样本 · 原始门槛未证实", B_relative_technical: "按技术分择强",
  C_preevent: "匹配事前事件", C_forced_without_verified_event: "强制样本 · 缺少事前事件证据",
};
const noFill: Record<string, string> = {
  no_candidate_at_information_cutoff: "截止时点无可用候选",
  no_affordable_opening_print_in_preranked_candidates: "候选无可用开盘成交或超出预算",
};

function SessionDetail({ session, openProduct }: { session: BacktestSession; openProduct: (symbol: string) => void }) {
  const { outcome } = session;
  return <div className="session-detail">
    <p className="muted">信息截止 {session.information_day} · 模拟执行 {session.day}</p>
    {outcome.product ? (
      <>
        <div className="session-focus"><button className="text-action" onClick={() => openProduct(outcome.product!)}>{outcome.product} · {outcome.contract} →</button><span className="tag">{entries[outcome.decision ?? ""] ?? "历史开仓"}</span></div>
        <dl className="detail-metrics"><div><dt>开仓支出</dt><dd>¥{currency(outcome.paid ?? 0)}</dd></div><div><dt>开仓费用</dt><dd>¥{currency(outcome.fee ?? 0)}</dd></div><div><dt>每腿手数</dt><dd>{outcome.quantity}</dd></div><div><dt>冻结候选</dt><dd>{outcome.candidate_count} 个</dd></div></dl>
        {outcome.event && <div className="event-note"><strong>{outcome.event.title}</strong>
          <p>公布时间 {outcome.event.release_bjt}（北京时间） · {outcome.event.transmission}</p>
          <a href={outcome.event.preannounced_source} target="_blank" rel="noreferrer">事前公告</a>
        </div>}
        <ul className="legs">{outcome.legs?.map((leg) => <li key={leg.symbol}><code>{leg.symbol}</code><span>开盘价 {currency(leg.open_price)}</span></li>)}</ul>
      </>
    ) : <p>当日未开仓。{outcome.reason ? noFill[outcome.reason] ?? "请查看逐日原始记录。" : ""}</p>}
    {session.exits.length > 0 && <div className="table-scroll"><table><caption>当日已平</caption><thead><tr><th>交易编号</th><th>退出条件</th><th>净盈亏</th></tr></thead><tbody>{session.exits.map((exit) => (
      <tr key={exit.trade_id}><td>{exit.trade_id}</td><td>{exitReasons[exit.reason] ?? exit.reason}</td><td className={tone(exit.net_pnl)}>{signed(exit.net_pnl)} 元</td></tr>
    ))}</tbody></table></div>}
  </div>;
}

export function StrategyPlans({ data, id, initialDay, onSelect, openProduct }: { data: WorkbenchBacktest | null; id: StrategyPlan["id"]; initialDay?: string; onSelect: (id: StrategyPlan["id"]) => void; openProduct: (product: string) => void }) {
  const [tradeDay, setTradeDay] = useState(initialDay ?? "");
  const plan = data?.plans.find((item) => item.id === id);
  const session = plan?.sessions?.find((item) => item.day === tradeDay) ?? plan?.sessions?.[plan.sessions.length - 1];
  const point = plan?.curve?.find((item) => item.day === session?.day);
  return <>
    <header className="page-heading"><div><h1>策略计划</h1><p>从入选条件看到具体的历史执行记录。</p></div><span className="tag paper-tag">历史模拟</span></header>
    {data && <div className="plan-tabs" aria-label="选择策略">{data.plans.map((item) => (
      <button key={item.id} type="button" aria-pressed={item.id === id} onClick={() => onSelect(item.id)}><PlanBadge id={item.id} />{item.name}</button>
    ))}</div>}
    {plan && <div className="plan-detail-grid">
      <article className="surface plan-detail">
        <div className="card-head"><PlanBadge id={plan.id} /><div><h2>{plan.name}</h2><p>{plan.style}</p></div></div>
        <p>{plan.thesis}</p>
        <dl className="plan-steps">{plan.steps.map(([label, text]) => <div key={label}><dt>{label}</dt><dd>{text}</dd></div>)}</dl>
        <p className="method-note">{plan.limit}</p>
      </article>
      <article className="surface plan-detail">
        <div className="section-heading"><h2>逐日执行</h2>{session && <select aria-label="回测交易日" value={session.day} onChange={(event) => setTradeDay(event.target.value)}>{plan.sessions?.map((item) => <option key={item.day}>{item.day}</option>)}</select>}</div>
        {session ? <SessionDetail session={session} openProduct={openProduct} /> : <p className="empty-note">暂无逐日执行记录。</p>}
        {point && <dl className="detail-metrics daily-result"><div><dt>当日收盘权益</dt><dd>¥{currency(point.equity)}</dd></div><div><dt>累计收益</dt><dd className={tone(point.return_pct)}>{pct(point.return_pct)}</dd></div><div><dt>持仓数量</dt><dd>{point.open_positions}</dd></div></dl>}
      </article>
      {!!plan.curve?.length && <section className="surface chart-surface wide" aria-label="本策略收益曲线">
        <div className="section-heading"><h2>本策略收益曲线</h2></div>
        <LineChart labels={["期初", ...plan.curve.map((item) => item.day)]}
          series={[{ label: plan.name, color: COLORS[plan.id], values: [0, ...plan.curve.map((item) => item.return_pct)] }]}
          format={pct} baseline={0} title={`${plan.name}逐日累计收益`} />
      </section>}
      {plan.stats && <article className="surface plan-detail wide"><h2>期末损益拆分</h2><dl className="detail-metrics">
        <div><dt>已实现净盈亏</dt><dd className={tone(plan.stats.realized)}>{signed(plan.stats.realized)} 元</dd></div>
        <div><dt>持仓浮盈亏</dt><dd className={tone(plan.stats.open_pnl)}>{signed(plan.stats.open_pnl)} 元</dd></div>
        <div><dt>已平 / 期末持有</dt><dd>{plan.stats.closed} / {plan.stats.open_positions}</dd></div>
        <div><dt>期末权益</dt><dd>¥{currency(plan.stats.equity)}</dd></div>
        <div><dt>最大回撤</dt><dd>{plan.stats.drawdown_pct.toFixed(2)}%</dd></div>
      </dl></article>}
    </div>}
  </>;
}
