import { useEffect, useState } from "react";
import { StrategyPage } from "./components/StrategyPage";
import { HistoryPage } from "./components/HistoryPage";

function route() {
  const [page, symbol, day] = window.location.hash.slice(1).split("/");
  const product = /^[a-z0-9]+$/i.test(symbol ?? "") ? symbol : "";
  return page === "history" ? {page, symbol:product, day:product && /^\d{4}-\d{2}-\d{2}$/.test(day ?? "") ? day : ""}
    : {page:"strategies", symbol:"", day:""};
}

export default function App() {
  const [current, setCurrent] = useState(route);
  useEffect(() => {
    function navigate() {
      const next = route();
      const hash = "#" + next.page + (next.symbol ? "/" + next.symbol : "") + (next.day ? "/" + next.day : "");
      if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
      setCurrent(next);
    }
    navigate();
    window.addEventListener("hashchange", navigate);
    return () => window.removeEventListener("hashchange", navigate);
  }, []);
  return <>
    <a className="skip" href="#content" onClick={event => {event.preventDefault(); document.getElementById("content")?.focus();}}>跳至正文</a>
    <header className="mast"><div className="mast-inner">
      <a className="brand" href="#strategies" aria-label="期策首页"><span className="brand-mark" aria-hidden="true">期</span>期策</a>
      <nav aria-label="工作台页面">
        <a href="#strategies" aria-current={current.page === "strategies" ? "page" : undefined}>策略总览</a>
        <a href="#history" aria-current={current.page === "history" ? "page" : undefined}>品种历史</a>
      </nav>
    </div></header>
    <main id="content" tabIndex={-1}>
      {current.page === "strategies" ? <StrategyPage /> : <HistoryPage symbol={current.symbol} requestedDay={current.day} />}
    </main>
  </>;
}
