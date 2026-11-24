import { useCallback, useEffect, useState } from "react";
import { getHistory, getProducts } from "../lib/api";
import { useVisibleResource } from "../lib/useVisibleResource";
import type { HistoryRecord } from "../types";
import { ResourceStatus } from "./ResourceStatus";

function RecordDetail({record}: {record: HistoryRecord}) {
  return <article className="record">
    <div className="record-meta"><span>{record.kind}</span>{record.at && <time dateTime={record.at}>{record.at.replace("T"," ")}</time>}</div>
    <h3>{record.title}</h3>
    <dl className="record-fields">{record.sections.map((section,i) =>
      <div key={i} className={section.label.length > 12 ? "long-field" : undefined}><dt>{section.label}</dt><dd>{section.text}</dd></div>)}</dl>
    {!!record.sources.length && <div className="sources"><h4>资料来源</h4><ul>{record.sources.map((source,i) =>
      <li key={i}>{/^(?:https?:\/\/|\/api\/)/i.test(source.url)
        ? <a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>
        : source.title || source.url}</li>)}</ul></div>}
  </article>;
}

function ProductRecords({symbol, requestedDay}: {symbol: string; requestedDay:string}) {
  const read = useCallback((signal: AbortSignal) => getHistory(symbol,signal), [symbol]);
  const {data, error, refresh} = useVisibleResource(read,true);
  const [day, setDay] = useState(requestedDay);
  useEffect(() => setDay(requestedDay), [requestedDay]);
  function chooseDay(value: string) {
    setDay(value);
    window.history.replaceState(null, "", "#history/" + symbol + "/" + value);
  }
  const days = [...new Set(data?.records.map(row => row.day) ?? [])].sort().reverse();
  const selected = days.includes(day) ? day : days[0];
  const index = days.indexOf(selected);
  const records = data?.records.filter(row => row.day === selected) ?? [];
  return <section className="panel records-panel" aria-label="品种分析详情">
    <ResourceStatus error={error} reload={refresh} pending={!data}/>
    {data && <>
      <div className="history-title"><div><p className="market-symbol">{data.exchange} · {data.product}</p>
        <h2>{data.name || data.product}</h2></div>
        <span className="quiet">{days.length} 个记录日 · {data.records.length} 条记录</span></div>
      {days.length > 0 ? <>
        <div className="day-picker"><label>记录日期<select value={selected} onChange={e => chooseDay(e.target.value)}>
          {days.map(value => <option key={value}>{value}</option>)}</select></label>
          <div className="day-buttons"><button disabled={index >= days.length - 1} onClick={() => chooseDay(days[index+1])}>前一日</button>
          <button disabled={index <= 0} onClick={() => chooseDay(days[index-1])}>后一日</button></div>
        </div>
        {records.map((record,i) => <RecordDetail key={selected + i} record={record}/>)}
      </> : <p className="empty">该品种暂无已归档的分析记录。</p>}
    </>}
  </section>;
}

export function HistoryPage({symbol, requestedDay}: {symbol: string; requestedDay:string}) {
  const {data, error, refresh} = useVisibleResource(getProducts,true);
  const [search, setSearch] = useState("");
  const products = data?.products ?? [];
  const query = search.trim().toLocaleLowerCase();
  const matching = products.filter(p => (p.name + " " + p.symbol + " " + p.exchange).toLocaleLowerCase().includes(query));
  const chosen = products.find(p => p.symbol.toUpperCase() === symbol.toUpperCase()) ?? products[0];
  return <>
    <header className="page-title"><h1>品种历史分析</h1>{data && <span className="as-of">{products.length} 个品种</span>}</header>
    <ResourceStatus error={error} reload={refresh} pending={!data}/>
    {data && (chosen ? <div className="history-layout">
      <aside className="panel product-list">
        <label className="field-label">查找品种<input type="search" placeholder="输入名称或代码" value={search} onChange={e => setSearch(e.target.value)}/></label>
        <ul aria-label="品种列表">{matching.map(p => <li key={p.symbol}>
          <a href={"#history/" + p.symbol} aria-current={p.symbol === chosen.symbol ? "true" : undefined}>
            <strong>{p.name}</strong><span>{p.symbol}</span></a>
        </li>)}</ul>
        {!matching.length && <p className="quiet">没有匹配的品种。</p>}
      </aside>
      <ProductRecords key={chosen.symbol} symbol={chosen.symbol} requestedDay={requestedDay}/>
    </div> : <p className="empty">暂无研究品种。</p>)}
  </>;
}
