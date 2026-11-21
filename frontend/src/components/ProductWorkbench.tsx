import { useEffect, useState } from "react";
import { getMarketQuote, getProductAnalysis } from "../lib/api";
import { formatTimestamp, humanStatus } from "../lib/model";
import { productWatch } from "../lib/productWatch";
import type { DashboardView, MarketQuote, ProductAnalysis, ProductEvidence, StrategyPlan, WorkbenchBacktest } from "../types";
import { LineChart } from "./LineChart";
import { currency, pct, PlanBadge } from "./StrategyWorkbench";

const priceLabel = (value: number) => currency(value);
const decisionLabels: Record<string, string> = { wait: "等待", futures: "期货研究", long_call: "买方看涨", long_put: "买方看跌" };
const availability: Record<string, string> = { complete: "有证据", partial: "部分证据", unobservable: "不可观测" };

function DomainEvidence({ title, data, empty }: { title: string; data?: ProductEvidence; empty: string }) {
  return <article className="surface evidence-card">
    <div className="section-heading"><h3>{title}</h3><span className="tag">{data ? `${availability[data.status] ?? humanStatus(data.status)} · ${humanStatus(data.freshness)}` : "待取证"}</span></div>
    <p>{data?.conclusion ?? empty}</p>
    {!!data?.sources.length && <details className="table-details"><summary>查看 {data.sources.length} 个来源</summary><ul>
      {data.sources.map((source, index) => <li key={`${source.url}-${index}`}><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a>
        <small>{formatTimestamp(source.retrieved_at)} · {source.locator}</small></li>)}
    </ul></details>}
  </article>;
}

export function ProductWorkbench({
  products, symbol, choose, startResearch, canRun, revision, backtest, openPlan, openArchive,
}: {
  products: DashboardView["products"];
  symbol: string;
  choose: (symbol: string) => void;
  startResearch: (symbol: string) => void;
  canRun: boolean;
  revision: number;
  backtest: WorkbenchBacktest | null;
  openPlan: (id: StrategyPlan["id"], day?: string) => void;
  openArchive: (id: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [analysis, setAnalysis] = useState<ProductAnalysis | null>(null);
  const [failure, setFailure] = useState("");
  const [quote, setQuote] = useState<MarketQuote | null>(null);
  const [quoteBusy, setQuoteBusy] = useState(false);
  const [quoteFailure, setQuoteFailure] = useState<{ symbol: string; message: string } | null>(null);
  const [windowSize, setWindowSize] = useState(60);

  useEffect(() => {
    if (!symbol) return;
    const controller = new AbortController();
    setFailure("");
    void getProductAnalysis(symbol, controller.signal).then((value) => {
      if (!controller.signal.aborted) setAnalysis(value);
    }).catch((error: Error) => { if (!controller.signal.aborted) setFailure(error.message); });
    return () => controller.abort();
  }, [symbol, revision]);

  const product = products.find((item) => item.symbol === symbol);
  const watch = productWatch[symbol];
  const record = analysis?.product === symbol ? analysis : null;
  const observation = record?.observation;
  const strategy = record?.analysis?.result?.strategy;
  const bars = (observation?.daily_bars ?? []).filter((row): row is typeof row & { close: number } => typeof row.close === "number" && row.close > 0).slice(-windowSize);
  const last = bars[bars.length - 1];
  const previous = bars[bars.length - 6];
  const week = last && previous ? (last.close / previous.close - 1) * 100 : null;
  const recent = bars.slice(-20);
  const high = recent.flatMap((row) => typeof row.high === "number" && row.high > 0 ? [row.high] : []);
  const low = recent.flatMap((row) => typeof row.low === "number" && row.low > 0 ? [row.low] : []);
  const average = recent.length === 20 ? recent.reduce((sum, bar) => sum + bar.close, 0) / 20 : null;
  const related = backtest?.plans.flatMap((plan) => (plan.sessions ?? []).filter((session) => session.outcome.product === symbol).map((session) => ({ plan, session }))) ?? [];
  const visibleProducts = products.filter((item) => `${item.symbol} ${item.name} ${item.exchange}`.toUpperCase().includes(search.trim().toUpperCase()));
  const loadQuote = async () => {
    setQuoteBusy(true); setQuoteFailure(null); setQuote(null);
    try { setQuote(await getMarketQuote(symbol)); }
    catch (error) { setQuoteFailure({ symbol, message: error instanceof Error ? error.message : "行情读取失败" }); }
    finally { setQuoteBusy(false); }
  };

  return <>
    <header className="page-heading"><div><h1>品种研究</h1><p>实际合约行情、证据来源和逐项分析。</p></div></header>
    <div className="product-layout">
      <aside className="surface product-list" aria-label="研究品种">
        <label htmlFor="symbol-search">品种 <span>{products.length}</span></label>
        <input id="symbol-search" placeholder="搜索名称、代码或交易所" value={search} onChange={(event) => setSearch(event.target.value)} />
        <div>{visibleProducts.map((item) => <button type="button" key={item.symbol} aria-pressed={item.symbol === symbol} onClick={() => choose(item.symbol)}>
          <span><strong>{item.name}</strong><code>{item.symbol}</code></span><small>{item.exchange}</small>
        </button>)}</div>
        {!visibleProducts.length && <p className="muted">没有匹配的品种。</p>}
      </aside>
      <div className="product-body">
        <div className="surface product-summary">
          <div className="section-heading"><div><p className="overline">{product?.exchange ?? "商品期货"} · {symbol}</p><h2>{product?.name ?? symbol}</h2></div>
            <button type="button" className="primary-action" onClick={() => startResearch(symbol)} disabled={!canRun || !symbol}>运行单品种分析</button></div>
          {failure && <p role="alert" className="alert">{failure}</p>}
          {!record && !failure && <p role="status">正在读取品种证据…</p>}
          <div className="product-quote"><strong>{observation ? currency(observation.price) : "—"}</strong><span>{observation ? `${observation.contract} · ${formatTimestamp(observation.observed_at)}` : "该品种暂无合约归档"}</span></div>
          <div className="inline-tools">
            {observation && <a href={observation.source} target="_blank" rel="noreferrer">报价来源</a>}
            {record?.evidence_run_id && <button className="text-action" type="button" onClick={() => openArchive(record.evidence_run_id!)}>查看取证记录 →</button>}
            <button className="text-action" type="button" onClick={() => void loadQuote()} disabled={quoteBusy}>{quoteBusy ? "查询中…" : "查最新连续报价"}</button>
          </div>
          {quote?.product === symbol && <p className="reference-quote">连续报价参考 <strong>{currency(quote.price)}</strong> · {formatTimestamp(quote.observed_at)} · <a href={quote.source_url} target="_blank" rel="noreferrer">来源</a></p>}
          {quoteFailure?.symbol === symbol && <p role="alert" className="alert">{quoteFailure.message}</p>}
          {strategy && <p className="reference-quote">归档判断 · <strong>{decisionLabels[strategy.decision] ?? strategy.decision}</strong> · {strategy.analysis_contract ?? symbol} · {formatTimestamp(strategy.as_of)}</p>}
        </div>
        <section className="surface chart-surface" aria-labelledby="product-chart">
          <div className="section-heading"><div><h2 id="product-chart">{observation?.contract ?? symbol} 收盘走势</h2><p>{observation?.daily_status === "verified" ? "已验证的完成日线" : "归档的同月单合约日线 · 研究参考"}</p></div>
            <div className="segmented" aria-label="日线窗口">{[20, 60, 120].map((size) => <button key={size} type="button" aria-pressed={size === windowSize} onClick={() => setWindowSize(size)}>{size} 日</button>)}</div>
          </div>
          <LineChart title={`${symbol} 历史收盘走势`} labels={bars.map((bar) => bar.date)} series={bars.length ? [{ label: "收盘价", color: "#235fb3", values: bars.map((bar) => bar.close) }] : []} format={priceLabel} />
          {last && <dl className="detail-metrics product-metrics"><div><dt>末个归档收盘</dt><dd>{currency(last.close)}</dd></div><div><dt>近 5 日变化</dt><dd>{week === null ? "—" : pct(week)}</dd></div><div><dt>20 日均线</dt><dd>{average === null ? "—" : currency(average)}</dd></div><div><dt>近 20 日最高</dt><dd>{high.length ? currency(Math.max(...high)) : "—"}</dd></div><div><dt>近 20 日最低</dt><dd>{low.length ? currency(Math.min(...low)) : "—"}</dd></div></dl>}
        </section>
        <section aria-labelledby="product-framework">
          <h2 id="product-framework" className="section-title">分析架构</h2>
          {strategy && <p className="muted">以下判断取自 {strategy.analysis_contract ?? symbol} 在 {formatTimestamp(strategy.as_of)} 的研究记录。</p>}
          <div className="evidence-grid">
            <article className="surface evidence-card"><div className="section-heading"><h3>合约与时点</h3><span className="tag">{observation ? "有归档" : "待取证"}</span></div>
              <p>{observation ? `实际月份 ${observation.contract}，于 ${formatTimestamp(observation.available_at)} 采集。` : "先确认实际月份、行情时间和可用交易时段。"}</p>
              {strategy?.session_daily_bar && <p>{strategy.session_daily_bar.bar_state === "session_partial" ? "策略使用部分日线，只支持相应时段的条件判断。" : "策略绑定完成日线。"}</p>}
            </article>
            <DomainEvidence title="技术结构" data={strategy?.technical_structure} empty="可先查看价格区间和近期收盘走势。触发、目标及结构失效位由本品种分析给出。" />
            <article className="surface evidence-card"><div className="section-heading"><h3>基本面观察项</h3><span className="tag">取证提纲</span></div>
              {watch ? <dl className="plan-steps"><div><dt>供应</dt><dd>{watch.supply}</dd></div><div><dt>需求</dt><dd>{watch.demand}</dd></div><div><dt>事件</dt><dd>{watch.events}</dd></div></dl> : <p>从供需、库存与事件日历建立观察项。</p>}
            </article>
            <DomainEvidence title="已取证的基本面与催化" data={strategy?.fundamentals_news} empty="待核对供需、库存与事件时点，区分已发生事实和预期。" />
            <DomainEvidence title="资金与持仓" data={strategy?.member_flow} empty="待核对实际月份的成交、持仓变化和可核实的会员证据。" />
            <DomainEvidence title="期权链与成本" data={strategy?.option_market} empty="待核对同月期权的行权价、盘口、到期和权利金。" />
            <article className="surface evidence-card"><div className="section-heading"><h3>结论、反证与失效</h3><span className="tag">{strategy ? "已归档" : "待分析"}</span></div>
              {strategy ? <><p>{strategy.rationale.thesis}</p><p>反证 · {strategy.quality_review.strongest_challenge}</p><p>{strategy.quality_review.resolution}</p>
                <dl className="detail-metrics"><div><dt>触发</dt><dd>{strategy.market_levels.trigger ?? "—"}</dd></div><div><dt>失效</dt><dd>{strategy.market_levels.invalidation ?? "—"}</dd></div><div><dt>目标</dt><dd>{strategy.market_levels.target ?? "—"}</dd></div></dl>
                {!!strategy.evidence_gaps.length && <p>证据缺口 · {strategy.evidence_gaps.join("、")}</p>}
                <p>复核时间 · {formatTimestamp(strategy.reassess_by)}</p>
              </> : <p>还没有本品种的归档结论。运行单品种分析后可查看取证、反证和失效条件。</p>}</article>
          </div>
        </section>
        {backtest?.status === "ready" && <section className="surface plan-detail">
          <div className="section-heading"><h2>三套计划中的历史记录</h2><span className="tag paper-tag">历史模拟</span></div>
          {!backtest.covered_products.includes(symbol) ? <p className="empty-note">这一批次缺少本品种连续可复核的期权链。</p> : !related.length ? <p className="empty-note">本批行情覆盖该品种，三组组合均未选中开仓。</p> : (
            <div className="table-scroll"><table><thead><tr><th>策略</th><th>开仓日</th><th>期权腿</th><th>模拟支出</th></tr></thead>
              <tbody>{related.map(({ plan, session }) => <tr key={`${plan.id}-${session.day}`}><td><button className="text-action" onClick={() => openPlan(plan.id, session.day)}><PlanBadge id={plan.id} /> {plan.name}</button></td><td>{session.day}</td><td>{session.outcome.legs?.map((leg) => leg.symbol).join(" / ")}</td><td>¥{currency(session.outcome.paid ?? 0)}</td></tr>)}</tbody></table></div>
          )}
        </section>}
      </div>
    </div>
  </>;
}
