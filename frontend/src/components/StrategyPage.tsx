import { useState } from "react";
import { getStrategies } from "../lib/api";
import { useVisibleResource } from "../lib/useVisibleResource";
import type { DecisionPlan, LegFill, Strategy, StrategyDecision, TradeFill } from "../types";
import { LineChart } from "./LineChart";
import { ResourceStatus } from "./ResourceStatus";

const colors = { A: "#2864b6", B: "#008570", C: "#ac601a" };
const amount = (value: number) => value.toLocaleString("zh-CN", {maximumFractionDigits: 2});
const percent = (value: number) => (value > 0 ? "+" : "") + value.toFixed(2) + "%";

function LegTable({fills, closing = false}: {fills: LegFill[]; closing?: boolean}) {
  return <div className="fill-table"><table aria-label={closing ? "各腿平仓成交" : "各腿开仓成交"}>
    <thead><tr><th scope="col">合约</th><th scope="col">手数</th>
      {closing ? <th scope="col">平仓净收入（元）</th> : <th scope="col">入场成本（元）</th>}
    </tr></thead>
    <tbody>{fills.map(fill => <tr key={fill.leg}><th scope="row">{fill.leg}</th><td>{fill.quantity}</td>
      <td>{(closing ? fill.proceeds_after_costs : fill.premium_and_fees)?.toLocaleString("zh-CN", {minimumFractionDigits:2,maximumFractionDigits:2}) ?? "待核实"}</td></tr>)}</tbody>
  </table></div>;
}

function SettledFills({fills}: {fills: TradeFill[]}) {
  const [day,setDay] = useState("");
  const ordered = [...fills].sort((a,b) => b.day.localeCompare(a.day));
  const trade = ordered.find(item => item.day === day) ?? ordered[0];
  if (!trade) return null;
  return <section className="settled-fills"><h4>双买成交明细</h4>
    <label className="fill-selector">开仓日期<select value={trade.day} onChange={e => setDay(e.target.value)}>
      {ordered.map(item => <option key={item.day} value={item.day}>{item.day} · {item.product}</option>)}
    </select></label>
    <LegTable fills={trade.leg_fills}/>
    <p className="fine">两腿实际入场成本合计 {amount(trade.premium_and_fees)} 元，含开仓费用。
      <a href={`#history/${trade.product}/${trade.day}`}>查看该日品种分析与成交记录</a></p>
  </section>;
}

function PlanDetails({plan}: {plan: DecisionPlan}) {
  const rows = [
    ["品种", plan.product], ["合约与方向", plan.legs.join(" / ")],
    ["决策依据", plan.thesis], ["技术面", plan.technical],
    ["基本面", plan.fundamentals], ["消息面", plan.news], ["持仓归属", plan.ownership],
    ["失效条件", plan.invalidation],
  ].filter(([, value]) => value);
  const terms = [
    ["预算（元）", plan.requested_budget_cny], ["持有期（交易日）", plan.holding_days],
    ["入场权利金上限", plan.max_entry_premium], ["手数上限", plan.max_lots],
    ["目标价", plan.scenario_target], ["下行情景", plan.scenario_low], ["上行情景", plan.scenario_high],
    ["收盘失效价", plan.invalidation_price], ["收盘止盈价", plan.take_profit_close],
  ] as const;
  return <>
      <dl>{rows.map(([label,value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      {!!plan.requested_budget_cny && <dl>{terms.filter(([,value]) => value != null && value > 0)
        .map(([label,value]) => <div key={label}><dt>{label}</dt><dd>{amount(value!)}</dd></div>)}</dl>}
    {!!plan.evidence?.length && <details><summary>决策证据</summary><ul>{plan.evidence.map((item,i) =>
      <li key={i}>{/^https?:\/\//i.test(item) ? <a href={item} target="_blank" rel="noreferrer">{item}</a> : item}</li>)}</ul></details>}
  </>;
}

function Decision({ decision, asOf, group }: {decision: StrategyDecision; asOf: string; group:string}) {
  const selected = decision.effective_order ?? decision;
  const execution = decision.execution;
  const executionLabels: Record<string,string> = {wait:"观望", blocked:"未开仓", unfilled:"未成交", simulated_open:"模拟开仓"};
  return <section className="decision">
    <div className="decision-head"><h4>当天决策</h4><time>{asOf.slice(0,10)}</time></div>
    {decision.day !== asOf.slice(0,10) ? <p>当天尚无已归档决策。</p> : <>
      <p className="decision-action">{selected.status || "待确认"}</p>
      {decision.mode && <p className="fine">{decision.mode === "historical_replay" ? "按当时可见证据回放" : "盘前归档"}</p>}
      {decision.effective_order && <p className="order-source">
        有效委托来自策略 {decision.order_source || group}。{decision.selection_reason}
      </p>}
      <PlanDetails plan={selected}/>
      {decision.effective_order && <details className="original-plan"><summary>模型原判 · {decision.status}{decision.product && " · " + decision.product}</summary>
        <PlanDetails plan={decision}/>
      </details>}
      {execution && <p className="execution">模拟执行 · {executionLabels[execution.status] ?? execution.status}
        {!execution.leg_fills?.length && execution.quantity != null && ` ${execution.quantity} 手`}
        {execution.premium_and_fees != null && `，入场成本 ${amount(execution.premium_and_fees)} 元`}
        {execution.reason && execution.reason !== decision.thesis && `。 ${execution.reason}`}</p>}
      {execution?.fill_method === "daily_limit_proxy" && <p className="limit-fill">日内限价按日线近似。开盘权利金 {execution.market_open_prices?.map(amount).join(" / ") ?? "待核实"}，
        日内最低 {execution.observed_daily_low != null ? amount(execution.observed_daily_low) : "待核实"}，
        含滑点模拟成交权利金 {execution.filled_premium_including_slippage != null ? amount(execution.filled_premium_including_slippage) : "待核实"}。</p>}
      {!!execution?.leg_fills?.length && <LegTable fills={execution.leg_fills}/>}
      {!!execution?.exits?.length && <ul>{execution.exits.map((exit,i) => <li key={i}>
        平仓 {exit.product}{!exit.leg_fills?.length && exit.quantity != null && ` ${exit.quantity} 手`}，盈亏 {amount(exit.pnl)} 元。{exit.reason}
        {!!exit.leg_fills?.length && <LegTable fills={exit.leg_fills} closing/>}
      </li>)}</ul>}
      {decision.cutoff && <p className="fine">证据截止 {decision.cutoff.replace("T"," ")}</p>}
    </>}
  </section>;
}

function StrategyDetail({strategy, asOf}: {strategy: Strategy; asOf: string}) {
  const validation = strategy.validation;
  const policyNames: Record<string, string> = {
    second_signal: "同品种新信号再入场",
    short_rotation: {A:"日内限价与短持",B:"日内限价、短持与备选委托",C:"事件后退出"}[strategy.id],
    day_limit: {A:"日内限价",B:"日内限价",C:"事件后退出"}[strategy.id],
  };
  const policy = policyNames[strategy.policy_id ?? ""];
  return <article id={"strategy-" + strategy.id} tabIndex={-1} className="strategy-card" style={{borderTopColor: colors[strategy.id]}}>
    <div className="strategy-info">
      <div className="strategy-key"><span className="swatch" style={{background:colors[strategy.id]}}/>策略 {strategy.id}</div>
      <h3>{strategy.name}</h3>
      <p className="fine version">版本 {strategy.version}{policy && ` · ${policy}`}</p>
      {strategy.policy_id && strategy.policy_id !== "opening" && validation && <p className="validation">
        历史分段复核（{validation.start} 至 {validation.end}）中，开仓由 {validation.baseline.opened} 笔增至 {validation.selected.opened} 笔，
        扣费净损益由 {amount(validation.baseline.net_pnl)} 元变为 {amount(validation.selected.net_pnl)} 元。
      </p>}
      <h4>执行规则</h4><ul className="rules">{strategy.rules.map((rule,i) => <li key={i}>{rule}</li>)}</ul>
    </div>
    <Decision decision={strategy.decision} asOf={asOf} group={strategy.id}/>
    {strategy.id === "C" && !!strategy.fills?.length && <SettledFills fills={strategy.fills}/>}
  </article>;
}

export function StrategyPage() {
  const {data, error, refresh} = useVisibleResource(getStrategies, true);
  const plans = data?.strategies ?? [];
  const days = [...new Set(plans.flatMap(plan => plan.curve.map(point => point.day)))].sort();
  const series = plans.map(plan => {
    const points = new Map(plan.curve.map(p => [p.day,p.return_pct]));
    return {label: `策略 ${plan.id}`, color:colors[plan.id], values:days.map(day => points.get(day) ?? null)};
  });
  const coincident = days.length > 0 && series.length > 1 && series.every(
    item => item.values.every((value,i) => value != null && value === series[0].values[i]));
  return <>
    <header className="page-title"><h1>策略总览</h1>{data && <span className="as-of">决策日期 <time>{data.as_of.slice(0,10)}</time></span>}</header>
    <ResourceStatus error={error} reload={refresh} pending={!data} />
    {data && <>
      <section className="panel chart-panel">
        <header className="panel-head"><div><h2>累计收益曲线</h2><p className="period">{data.start} — {data.end ?? "等待首个日结"}</p></div>
          <div className="budgets">
            <div className="budget"><span>A / B 单笔预算</span><strong>¥ {amount(data.trade_budget)}</strong></div>
            <div className="budget"><span>C 双买 · 每腿预算</span><strong>{data.per_leg_budget != null ? "¥ " + amount(data.per_leg_budget) : "待更新"}</strong></div>
          </div>
        </header>
        <div className="returns">{plans.map(plan => {
          const point = plan.curve.find(p => p.day === data.end);
          return <div className="return" key={plan.id}>
            <span className="legend-label"><i className="swatch" style={{background:colors[plan.id]}}/>策略 {plan.id}</span>
            <strong style={{color:colors[plan.id]}}>{point?.return_pct != null ? percent(point.return_pct) : "待估值"}</strong>
            <span className="return-name">{plan.name}</span>
            <span className="trade-count">开仓 {plan.opened} 笔 · 平仓 {plan.closed} 笔</span>
            <button className="jump" onClick={() => document.getElementById("strategy-" + plan.id)?.focus()}>
              今日 · {plan.decision.day === data.as_of.slice(0,10) ? (plan.decision.effective_order?.status ?? plan.decision.status) : "待归档"} ↓
            </button>
          </div>;
        })}</div>
        <LineChart labels={days} series={series} baseline={0} format={percent} title="三策略逐日累计收益率" />
        {coincident && <p className="fine">三个策略在已有估值日的累计收益率相同，曲线重合。</p>}
        <p className="method">{data.method}</p>
      </section>
      {!!data.issues.length && <aside className="notice"><strong>数据覆盖</strong><ul>{data.issues.map((issue,i) => <li key={i}>{issue}</li>)}</ul></aside>}
      <h2 className="section-title">策略内容与当天决策</h2>
      <div className="strategy-grid">{plans.map(plan => <StrategyDetail key={plan.id} strategy={plan} asOf={data.as_of}/>)}</div>
    </>}
  </>;
}
