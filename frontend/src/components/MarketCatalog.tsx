import { useState } from "react";
import { getMarketQuote } from "../lib/api";
import type { DashboardView, MarketQuote } from "../types";

export function MarketCatalog({ products }: { products: DashboardView["products"] }) {
  const [quote, setQuote] = useState<MarketQuote | null>(null);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");

  const lookup = async (symbol: string) => {
    setPending(symbol);
    setQuote(null);
    setError("");
    try {
      setQuote(await getMarketQuote(symbol));
    } catch (reason) {
      setError(`${symbol} · ${reason instanceof Error ? reason.message : "行情读取失败。"}`);
    } finally {
      setPending("");
    }
  };

  return (
    <section className="product-catalog" aria-labelledby="catalog-title">
      <h2 id="catalog-title">研究品种</h2>
      <p>点击品种查看公开连续行情。</p>
      <div className="catalog-quote" aria-live="polite">
        {pending && <span>正在查询 {pending}…</span>}
        {error && <span role="alert">{error}</span>}
        {quote && (
          <>
            <strong>{quote.product} · {quote.price}</strong>
            <span>参考报价 · 北京时间 {quote.observed_at.slice(0, 19).replace("T", " ")}</span>
            <a href={quote.source_url} target="_blank" rel="noreferrer">行情来源</a>
          </>
        )}
      </div>
      <ul>
        {products.map(({ symbol, name, exchange }) => (
          <li key={symbol}>
            <button type="button" disabled={!!pending} onClick={() => void lookup(symbol)}>
              <code>{symbol}</code><span>{name}</span><small>{exchange}</small>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
