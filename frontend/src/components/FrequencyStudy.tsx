import { useEffect, useState } from "react";
import { getFrequencyStudy, getFrequencySession } from "../lib/api";
import { LineChart } from "./LineChart";
import { currency, pct } from "./StrategyWorkbench";
import type { FrequencyResult, FrequencySession, ResearchMode } from "../types";

const modes = {
  baseline: "上一版",
  primary: "新版首选",
  ranked: "新版主备",
} as const;
const colors = { baseline: "#758297", primary: "#345da8", ranked: "#057456" };
const groups = {
  A: "多因子方向买权",
  B: "技术择强买权",
  C: "事件驱动双买",
} as const;
const periodNames = {
  full: "全期",
  initial: "前83日",
  holdout: "后42日",
} as const;
const number = (v: number | null, unit = "") =>
  v == null ? "—" : v.toFixed(2) + unit;

function DailyDecision({
  group,
  rows,
  selected,
}: {
  group: keyof typeof groups;
  rows: FrequencyResult["rows"];
  selected: ResearchMode;
}) {
  const dates = rows[0].curve.map((p) => p.day);
  const [day, setDay] = useState(dates[dates.length - 1]);
  const [variant, setVariant] = useState(selected);
  const [session, setSession] = useState<FrequencySession | null>(null);
  const [failure, setFailure] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setSession(null);
    setFailure("");
    getFrequencySession(variant, group, day, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setSession(data);
      })
      .catch((err) => {
        if (!controller.signal.aborted)
          setFailure(err instanceof Error ? err.message : "逐日决策读取失败");
      });
    return () => controller.abort();
  }, [day, variant, group]);
  const opened = rows.find((r) => r.variant === variant)?.open_days ?? [];
  const candidates = session?.rankings
    ? [
        session.rankings.primary,
        ...(variant === "ranked" && session.rankings.reserve
          ? [session.rankings.reserve]
          : []),
      ]
    : session
      ? [session.decision]
      : [];
  return (
    <div className="surface plan-detail">
      <div className="section-heading">
        <h2>当日计划与成交</h2>
        <div className="toolbar">
          <select
            aria-label="决策版本"
            value={variant}
            onChange={(e) => setVariant(e.target.value as ResearchMode)}
          >
            {Object.entries(modes).map(([v, name]) => (
              <option key={v} value={v}>
                {name}
              </option>
            ))}
          </select>
          <select
            aria-label="决策日期"
            value={day}
            onChange={(e) => setDay(e.target.value)}
          >
            {dates.map((d) => (
              <option key={d} value={d}>
                {d}
                {opened.includes(d) ? " · 模拟开仓" : ""}
              </option>
            ))}
          </select>
        </div>
      </div>
      {failure && <p role="alert">{failure}</p>}
      {session && (
        <>
          <p>
            证据截止 {session.cutoff.replace("T", " ")}。
            {session.account_before && (
              <>
                模拟现金 {currency(session.account_before.cash)} 元，在手成本{" "}
                {currency(session.account_before.open_cost)} 元。
              </>
            )}
          </p>
          {candidates.map((d, i) => (
            <details open={i === 0} key={i}>
              <summary>
                {i ? "备选" : "首选"} · {d.product || "全市场"} ·{" "}
                {d.action === "enter" ? "条件入场" : "等待"}
              </summary>
              <p>{d.thesis}</p>
              <dl className="plan-steps">
                {[
                  ["技术面", d.technical],
                  ["基本面", d.fundamentals],
                  ["消息面", d.news],
                  ["持仓归属", d.ownership],
                  ["失效条件", d.invalidation],
                ].map(([k, v]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
              </dl>
              {d.legs.length > 0 && (
                <p>
                  合约 {d.legs.join(" / ")} · 单笔预算 {pct(d.risk_bps / 100)} ·
                  最多持有 {d.holding_days} 个交易日
                </p>
              )}
            </details>
          ))}
          {session.screen?.map((r) => (
            <p key={r.rank}>
              {r.rank === 1 ? "首选筛查" : "备选筛查"} · {r.product} {r.reason}
            </p>
          ))}
          <p>
            <strong>
              {session.execution.status === "simulated_open"
                ? "模拟开仓"
                : "当日未开仓"}
            </strong>{" "}
            · {session.execution.reason}
          </p>
          {session.execution.exits.map((e, i) => (
            <p key={i}>
              平仓 {e.product} · 净损益 {currency(e.pnl)} 元 · {e.reason}
            </p>
          ))}
          <a
            href={`/api/workbench/agent-replay/context/${day}`}
            target="_blank"
            rel="noreferrer"
          >
            核对该日 22 个品种的冻结证据
          </a>
        </>
      )}
      {!failure && !session && <p role="status">正在读取该日计划…</p>}
    </div>
  );
}

export function FrequencyStudy() {
  const [result, setResult] = useState<FrequencyResult | null>(null);
  const [error, setError] = useState("");
  const [group, setGroup] = useState<keyof typeof groups>("B");
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      try {
        const data = await getFrequencyStudy(abort.signal);
        if (abort.signal.aborted) return;
        setResult(data);
        setError("");
        if (data.status === "pending") timer = setTimeout(refresh, 30000);
      } catch (err) {
        if (!abort.signal.aborted)
          setError(err instanceof Error ? err.message : "读取实验失败");
      }
    };
    void refresh();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, []);
  const [pairKey, setPairKey] = useState(0);
  const [anchor, setAnchor] = useState("option");
  const useOptions = anchor === "option" && Boolean(result?.option_anchor);
  const term = useOptions ? result?.option_anchor?.term : result?.term;
  const pairs =
    (useOptions ? result?.option_anchor?.pairs : result?.term_pairs) ?? [];
  const pair = pairs[pairKey];
  const rows = result?.rows.filter((r) => r.group === group) ?? [];
  return (
    <>
      <header className="page-heading">
        <div>
          <h1>出手频率与期权期限</h1>
          <p>2026-05-21 至 2026-11-20 · 单策略期初 20 万元 · 含滑点及费用</p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {!result && !error && <p role="status">正在读取本地实验…</p>}
      {result && (
        <>
          {result.status === "pending" && (
            <p role="status" className="alert">
              本机实验尚未齐全。已登记 {result.completed_days}{" "}
              个交易日的新版判断。
            </p>
          )}
          {rows.length > 0 && (
            <section className="surface plan-detail">
              <div className="section-heading">
                <h2>Agent 策略对照</h2>
                <select
                  aria-label="对照策略"
                  value={group}
                  onChange={(e) =>
                    setGroup(e.target.value as keyof typeof groups)
                  }
                >
                  {Object.entries(groups).map(([id, name]) => (
                    <option key={id} value={id}>
                      {id} {name}
                    </option>
                  ))}
                </select>
              </div>
              <p>
                当前研究模式为
                <strong> {modes[result.per_group[group]]} </strong>
                。主备模式每天最多新开一笔，首选因已有仓位或预算不可行时才评估备选。
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>版本</th>
                      <th>开仓</th>
                      <th>全期净损益（元）</th>
                      <th>后42日净损益（元）</th>
                      <th>最大回撤</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.variant}>
                        <td>{modes[r.variant]}</td>
                        <td>{r.opened}</td>
                        <td>{currency(r.pnl)}</td>
                        <td>{currency(r.tail_pnl)}</td>
                        <td>{pct(r.drawdown_pct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <LineChart
                title="三版累计收益率"
                labels={["期初", ...rows[0].curve.map((p) => p.day)]}
                baseline={0}
                format={pct}
                series={rows.map((r) => ({
                  label: modes[r.variant],
                  color: colors[r.variant],
                  values: [
                    0,
                    ...r.curve.map((p) => (p.equity / 200000 - 1) * 100),
                  ],
                }))}
              />
              <p className="method-note">
                升级要求交易增加、全期与后段净损益同时改善，最大回撤增加不超过
                0.5 个百分点。后段沿用已有持仓，属于同一历史区间。
                <a
                  href="/api/workbench/frequency-study/replay"
                  target="_blank"
                  rel="noreferrer"
                >
                  逐日决策与撮合记录
                </a>
              </p>
            </section>
          )}
          {rows.length > 0 && (
            <DailyDecision
              key={group}
              group={group}
              rows={rows}
              selected={result.per_group[group]}
            />
          )}
          {result.control.length > 0 && (
            <section className="surface plan-detail">
              <h2>只改变出手频率</h2>
              <p>
                固定趋势择强信号、0.5%
                单笔预算和三日持有期。低频组每五个交易日可入场一次，展示全部五种起始日；高频组每天都可尝试。
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>允许入场日</th>
                      <th>开仓</th>
                      <th>净损益（元）</th>
                      <th>后42日（元）</th>
                      <th>最大回撤</th>
                      <th>日均在手成本 / 净值</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.control.map((r) => (
                      <tr key={r.id}>
                        <td>
                          {r.id === "daily"
                            ? "每个交易日"
                            : "每五日 · 第" +
                              (Number(r.id.slice(-1)) + 1) +
                              "日起"}
                        </td>
                        <td>{r.opens}</td>
                        <td>{currency(r.pnl)}</td>
                        <td>{currency(r.tail_pnl)}</td>
                        <td>{pct(r.drawdown_pct)}</td>
                        <td>{pct(r.mean_cost_pct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <a
                href="/api/workbench/frequency-study/control"
                target="_blank"
                rel="noreferrer"
              >
                逐日信号与账户
              </a>
            </section>
          )}
          {term && (
            <section className="surface plan-detail">
              <div className="section-heading">
                <h2>事件双买 · 主力月与远月</h2>
                {result.option_anchor && (
                  <select
                    aria-label="主力月定义"
                    value={anchor}
                    onChange={(e) => {
                      setAnchor(e.target.value);
                      setPairKey(0);
                    }}
                  >
                    <option value="option">期权全链持仓量最大月</option>
                    <option value="future">期货持仓量最大月</option>
                  </select>
                )}
              </div>
              <p>
                同一事件、同一品种，两个月份各用对应的期货合约选择两腿。
                {result.term_mode === "compare_only"
                  ? "保留主力月为默认；远月用于对照。"
                  : "允许将满足成本与流动性条件的远月纳入研究候选。"}
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>窗口</th>
                      <th>配对样本</th>
                      <th>独立事件时点</th>
                      <th>主力收益</th>
                      <th>远月收益</th>
                      <th>远月差额（百分点）</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(periodNames).map(([key, label]) => {
                      const row = term[key as keyof typeof periodNames];
                      return (
                        <tr key={key}>
                          <td>{label}</td>
                          <td>{row.paired_products}</td>
                          <td>{row.event_clusters}</td>
                          <td>{number(row.main_mean_return_pct, "%")}</td>
                          <td>{number(row.far_mean_return_pct, "%")}</td>
                          <td>{number(row.far_minus_main_pp)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="method-note">
                每个月份按净损益除以 2,000
                元风险预算计算收益。先对同一事件的品种取平均，再对事件等权。两个期限都能模拟开平仓的样本才进入比较。{" "}
                <a
                  href={
                    useOptions
                      ? "/api/workbench/frequency-study/term-option"
                      : "/api/workbench/frequency-study/term"
                  }
                  target="_blank"
                  rel="noreferrer"
                >
                  事件、合约与两腿成交记录
                </a>
              </p>
              {term.one_day_decay && term.one_day_decay.paired_products > 0 && (
                <p>
                  假设一天内期货价格和隐含波动率不变，主力月理论损耗为两腿权利金的{" "}
                  {number(term.one_day_decay.main_pct_premium, "%")}，远月为{" "}
                  {number(term.one_day_decay.far_pct_premium, "%")}
                  。按实际组数折合单笔预算分别为{" "}
                  {number(term.one_day_decay.main_pct_budget, "%")} 和{" "}
                  {number(term.one_day_decay.far_pct_budget, "%")}
                  。用前收与真实到期日按零利率 Black-76 估算，未计提前行权。
                </p>
              )}
              {pair && (
                <>
                  <label>
                    配对明细{" "}
                    <select
                      aria-label="配对明细"
                      value={pairKey}
                      onChange={(e) => setPairKey(Number(e.target.value))}
                    >
                      {pairs.map((p, i) => (
                        <option key={p.event_at + p.product} value={i}>
                          {p.event_at.slice(0, 10)} · {p.product}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p>
                    模拟开仓 {pair.entry} · 模拟平仓 {pair.exit}
                  </p>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>月份</th>
                          <th>两腿合约</th>
                          <th>组数</th>
                          <th>入场总成本（元）</th>
                          <th>净损益（元）</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(["main", "far"] as const).map((k) => (
                          <tr key={k}>
                            <td>{pair[k].contract}</td>
                            <td>{pair[k].legs.map((l) => l.id).join(" / ")}</td>
                            <td>{pair[k].planned_quantity}</td>
                            <td>{currency(pair[k].execution.entry_cash)}</td>
                            <td>{currency(pair[k].execution.pnl_yuan)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </section>
          )}
        </>
      )}
    </>
  );
}
