import { useEffect, useState } from "react";
import { getFrozenContext } from "../lib/api";
import { formatTimestamp } from "../lib/model";
import type { FrozenContext } from "../types";

export function FrozenProduct({
  day,
  candidate,
  names,
}: {
  day: string;
  candidate: string;
  names: Record<string, string>;
}) {
  const [packet, setPacket] = useState<FrozenContext | null>(null);
  const [chosen, setChosen] = useState("");
  const [failure, setFailure] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setFailure("");
    void getFrozenContext(day, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setPacket(value);
      })
      .catch((error: Error) => {
        if (!controller.signal.aborted) setFailure(error.message);
      });
    return () => controller.abort();
  }, [day]);
  const current = packet?.trade_date === day ? packet : null;
  const products = Object.keys(current?.products ?? {});
  const preferred = chosen || candidate;
  const product = products.includes(preferred) ? preferred : products[0];
  const row = current?.products[product];
  const technical = row?.technical;
  const documents =
    current?.documents.filter((d) => d.products.includes(product)) ?? [];
  return (
    <section className="surface plan-detail">
      <div className="section-heading">
        <h2>品种历史证据</h2>
        {row && (
          <select
            aria-label="历史证据品种"
            value={product}
            onChange={(e) => setChosen(e.target.value)}
          >
            {products.map((code) => (
              <option key={code} value={code}>
                {code} {names[code]}
              </option>
            ))}
          </select>
        )}
      </div>
      {failure && <p role="alert">{failure}</p>}
      {!failure && !current && <p role="status">正在读取当日证据…</p>}
      {current && row && (
        <>
          <p className="muted">
            {day} 决策 · 行情截至 {current.daily_data_date}
          </p>
          <dl className="plan-steps">
            <div>
              <dt>技术面</dt>
              <dd>
                {technical ? (
                  <>
                    {technical.contract} 收盘 {technical.close.toLocaleString()}{" "}
                    · 成交 {technical.volume.toLocaleString()} 手 · 持仓{" "}
                    {technical.open_interest.toLocaleString()} 手<br />
                    五日涨跌{" "}
                    {technical.change5_pct == null
                      ? "缺样本"
                      : technical.change5_pct + "%"}{" "}
                    · 二十日年化波动率{" "}
                    {technical.realized_vol20_ann_pct == null
                      ? "缺样本"
                      : technical.realized_vol20_ann_pct + "%"}
                  </>
                ) : (
                  "当日缺少可核验的合约日行情。"
                )}
              </dd>
            </div>
            <div>
              <dt>期权候选</dt>
              <dd>
                {row.options.length
                  ? row.options.map((leg) => (
                      <div key={leg.id}>
                        {leg.id} · 收盘 {leg.premium} · 成交 {leg.volume} 手
                      </div>
                    ))
                  : "无满足当日筛选条件的已核验期权腿。"}
              </dd>
            </div>
            <div>
              <dt>持仓口径</dt>
              <dd>
                {Object.entries(row.monthly_aggregate).map(([kind, item]) => (
                  <div key={kind}>
                    {item.month} {kind === "future" ? "期货" : "期权"}月末总持仓{" "}
                    {item.month_end_open_interest.toLocaleString()} 手
                  </div>
                ))}
                {current.ownership}
              </dd>
            </div>
            {(
              [
                ["fundamentals", "基本面"],
                ["news", "消息面"],
              ] as const
            ).map(([dimension, label]) => (
              <div key={dimension}>
                <dt>{label}</dt>
                <dd>
                  {documents.filter((f) => f.dimension === dimension).length ? (
                    <ul>
                      {documents
                        .filter((f) => f.dimension === dimension)
                        .map((f) => (
                          <li key={f.id}>
                            {f.excerpt}
                            <br />
                            <small>
                              {f.id} ·{" "}
                              {f.freshness_role ===
                              "low_frequency_background_only"
                                ? "发行人季报背景"
                                : f.scope === "direct"
                                  ? "品种直接资料"
                                  : "行业或关联参考"}{" "}
                              · 可用时点 {formatTimestamp(f.available_at)} ·{" "}
                              <a href={f.url} target="_blank" rel="noreferrer">
                                {f.publisher}原文
                              </a>
                              {f.sha256 && (
                                <>
                                  {" "}
                                  ·{" "}
                                  <a
                                    href={`/api/workbench/agent-replay/context/${day}/source/${f.sha256}`}
                                    download
                                  >
                                    下载归档
                                  </a>
                                </>
                              )}
                            </small>
                          </li>
                        ))}
                    </ul>
                  ) : (
                    "截止当时暂无符合来源要求的资料。"
                  )}
                </dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </section>
  );
}
