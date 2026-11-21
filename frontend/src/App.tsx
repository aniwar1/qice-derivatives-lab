import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AsyncState } from "./components/AsyncState";
import { DecisionRail } from "./components/DecisionRail";
import { HelpTip } from "./components/HelpTip";
import { MarketCatalog } from "./components/MarketCatalog";
import { createStrategyRun, getDashboard, getStrategyRun, subscribeToRun } from "./lib/api";
import { directionLabel, formatTimestamp, humanStatus, normalizeDashboard } from "./lib/model";
import type { DashboardView, RunEvent, StrategyRunAccepted } from "./types";

type LoadState = "loading" | "ready" | "error";

const directionArrow: Record<string, string> = {
  long: "↗",
  short: "↘",
  neutral: "→",
  uncertain: "…",
  wait: "Ⅱ",
};

const domainNames: Record<string, string> = {
  market_contract: "合约身份",
  technical_structure: "技术结构",
  fundamentals_news: "基本面与新闻",
  member_flow: "会员资金",
  option_chain: "期权链",
  account_position: "账户持仓",
  realized_trade: "真实成交",
};

const optionActionLabel = (action: string): string => {
  const labels: Record<string, string> = {
    buy_to_open: "买入开仓",
    wait: "等待",
    hold: "持有",
    close: "平仓",
    not_selected: "不使用期权",
  };
  return labels[action] ?? action.replaceAll("_", " ");
};

function App() {
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [dashboard, setDashboard] = useState<DashboardView | null>(null);
  const [error, setError] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [runMessage, setRunMessage] = useState("");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [product, setProduct] = useState("");
  const closeStream = useRef<null | (() => void)>(null);
  const terminalRunId = useRef<string | null>(null);

  const load = useCallback(async () => {
    setError("");
    setLoadState((current) => (current === "ready" ? current : "loading"));
    try {
      const payload = await getDashboard();
      setDashboard(normalizeDashboard(payload));
      setLoadState("ready");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "本地服务未响应");
      setLoadState("error");
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      closeStream.current?.();
      closeStream.current = null;
    };
    // The initial request intentionally runs once; refreshes are explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const finishRun = useCallback(
    async (event: RunEvent, fallbackRunId?: string) => {
      const runId = typeof event.run_id === "string" ? event.run_id : fallbackRunId;
      // SSE and the status poll intentionally race.  Only the first terminal
      // signal may close the stream and reload the dashboard.
      if (runId && terminalRunId.current === runId) return;
      if (runId) terminalRunId.current = runId;
      closeStream.current?.();
      closeStream.current = null;
      setActiveRunId(null);
      if (event.type === "failed") {
        setError(event.message || "策略更新失败，请检查证据来源后重试。");
        setRunMessage("");
        setIsRunning(false);
        return;
      }
      setRunMessage("策略已更新");
      setIsRunning(false);
      await load();
    },
    [load],
  );

  const startRun = useCallback(async () => {
    if (isRunning) return;
    setError("");
    setIsRunning(true);
    setRunMessage("正在提交手动更新");
    try {
      const accepted = await createStrategyRun({
        mode: product ? "single_analysis" : "all_market_scan",
        ...(product ? { product } : {}),
        option_analysis_requested: true,
      });
      terminalRunId.current = null;
      setRunMessage(accepted.status === "queued" ? "策略已进入队列" : "正在启动 Codex");
      setActiveRunId(accepted.run_id);
    } catch (reason) {
      setIsRunning(false);
      setRunMessage("");
      setError(reason instanceof Error ? reason.message : "无法启动策略更新");
      if (!dashboard) setLoadState("error");
    }
  }, [dashboard, isRunning, product]);

  // A resident service may have started a run before the page connected.
  // Adopt that run and use the same tracking path as a manual click.
  useEffect(() => {
    const runId = dashboard?.runId;
    if (!runId || !["queued", "running"].includes(dashboard.runPhase)) return;
    setActiveRunId((current) => current ?? runId);
  }, [dashboard?.runId, dashboard?.runPhase]);

  // EventSource is useful for instant progress, but it is not a durable task
  // channel: browsers can reconnect, suspend a tab, or drop a stream.  Poll
  // the authoritative run row until a terminal state is observed so an
  // update can never remain visually stuck just because SSE was interrupted.
  useEffect(() => {
    if (!activeRunId) return;
    let cancelled = false;
    let timer: number | undefined;

    const check = async () => {
      try {
        const run = await getStrategyRun(activeRunId);
        if (cancelled) return;
        if (run.status === "completed") {
          await finishRun(
            { type: "completed", run_id: activeRunId, message: "策略已更新。" },
            activeRunId,
          );
          return;
        }
        if (run.status === "failed") {
          await finishRun(
            {
              type: "failed",
              run_id: activeRunId,
              message: run.error_message || "策略更新失败，旧策略保持不变。",
            },
            activeRunId,
          );
          return;
        }
        if (run.status === "queued") {
          setRunMessage((current) => current || "策略已进入队列");
        }
      } catch {
        if (!cancelled) {
          setRunMessage("实时进度暂时中断，正在查询任务状态…");
        }
      }
      if (!cancelled) timer = window.setTimeout(check, 2000);
    };

    void check();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [activeRunId, finishRun]);

  // Attach the live stream after activeRunId is known.  A connection error is
  // informational only; the browser may reconnect and the status poll above
  // remains authoritative.
  useEffect(() => {
    if (!activeRunId) return;
    const runId = activeRunId;
    const phase = dashboard?.runId === runId ? dashboard.runPhase : "queued";
    setIsRunning(true);
    setRunMessage((current) => current || (phase === "queued" ? "策略已进入队列" : "Codex 正在更新策略"));
    const accepted: StrategyRunAccepted = {
      run_id: runId,
      status: phase,
      events_url: `/api/runs/${runId}/events`,
    };
    const close = subscribeToRun(
      accepted,
      (event) => {
        if (event.run_id && event.run_id !== activeRunId) return;
        if (event.message) setRunMessage(event.message);
        if (event.type === "completed" || event.type === "failed") {
          void finishRun(event, activeRunId);
        }
      },
      () => {
        setRunMessage("实时进度暂时中断，任务仍由本地服务继续运行；正在查询状态…");
      },
    );
    closeStream.current = close;
    return () => {
      close();
      if (closeStream.current === close) closeStream.current = null;
    };
  }, [activeRunId, dashboard?.runId, dashboard?.runPhase, finishRun]);

  const activeNodeId = useMemo(
    () => dashboard?.nodes.find((node) => ["active", "in_progress", "blocked"].includes(node.status))?.id,
    [dashboard],
  );

  const showLinkedPanel = useCallback((nodeId: string) => {
    const targetByNode: Record<string, string> = {
      intent_router: "latest-strategy",
      scope_gate: "latest-strategy",
      market_scan: "evidence-panel",
      contract_binding: "strategy-panel",
      evidence_fanout: "evidence-panel",
      evidence_validation: "evidence-panel",
      agent_hypothesis: "strategy-panel",
      entry_path_comparison: "strategy-panel",
      option_expression: "option-panel",
      bounded_critic: "risk-panel",
      final_validation: "strategy-panel",
      position_lifecycle: "latest-strategy",
      unified_dashboard: "latest-strategy",
      learning_review: "risk-panel",
    };
    const element = document.getElementById(targetByNode[nodeId] ?? "latest-strategy");
    if (!element) return;
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    element.classList.remove("architecture-linked");
    window.requestAnimationFrame(() => element.classList.add("architecture-linked"));
    window.setTimeout(() => element.classList.remove("architecture-linked"), 900);
  }, []);

  if (loadState === "loading") return <AsyncState kind="loading" />;
  if (loadState === "error" && !dashboard)
    return <AsyncState kind="error" message={error} onAction={() => void load()} />;

  const codexNeedsLogin = dashboard?.codexState === "authentication_required";
  const header = (
    <WorkbenchHeader
      dashboard={dashboard}
      isRunning={isRunning}
      runMessage={runMessage}
      product={product}
      onSelectProduct={setProduct}
      onUpdate={() => void startRun()}
    />
  );

  if (!dashboard?.strategy) {
    const manualRefreshDue = dashboard?.strategyState === "stale" || dashboard?.strategyState === "expired";
    const invalid = dashboard?.strategyState === "invalid";
    const unavailableTitle = manualRefreshDue ? "等待手动更新" : invalid ? "上一版策略已停止公开" : undefined;
    const unavailableMessage = manualRefreshDue
      ? "上一版策略不会自动隐藏；需要新证据时，点击右上角按钮手动更新。"
      : invalid
        ? "存量策略没有通过当前契约校验，请重新生成。"
        : codexNeedsLogin
          ? "可先浏览研究品种。生成策略前，请在终端运行 codex login，然后重新加载页面。"
          : "选择研究范围，再手动生成第一版策略。";
    return (
      <div className="app-shell">
        {header}
        <div className="workbench">
          {error && <div className="inline-error" role="alert">{error}</div>}
          <AsyncState
            kind="empty"
            title={unavailableTitle}
            message={isRunning ? runMessage || "Codex 正在生成第一版策略。" : unavailableMessage}
            onAction={codexNeedsLogin ? undefined : () => void startRun()}
            busy={isRunning}
          />
          {!!dashboard?.products.length && <MarketCatalog products={dashboard.products} />}
          {dashboard && <DecisionRail nodes={dashboard.nodes} activeId={activeNodeId} onNodeActivate={showLinkedPanel} />}
        </div>
      </div>
    );
  }

  const { strategy, bar } = dashboard;
  const partialBar = bar.state === "partial_night" || bar.state === "partial_day";

  return (
    <div className="app-shell">
      <a className="skip-link" href="#latest-strategy">跳到最新策略</a>
      {header}

      <main className="workbench" id="latest-strategy">
        {error && (
          <div className="inline-error" role="alert">
            <strong>更新未完成</strong>
            <span>{error}</span>
            <button type="button" onClick={() => void startRun()}>重试</button>
          </div>
        )}

        {isRunning && (
          <div className="run-progress" role="status" aria-live="polite">
            <span className="run-progress__dot" aria-hidden="true" />
            <span>{runMessage || "Codex 正在处理更新…"}</span>
            <HelpTip label="解释更新状态">
              更新由本地服务代为调用 Codex；实时事件中断时会自动查询任务状态，完成或失败都会解除等待。
            </HelpTip>
          </div>
        )}

        <section className={`strategy-focus direction-${strategy.direction}`} id="strategy-panel" aria-labelledby="strategy-title">
          <div className="strategy-lead">
            <div className="strategy-overline">
              <span>最新策略</span>
              <span className="freshness-stamp">{formatTimestamp(dashboard.generatedAt)}</span>
              {(dashboard.strategyState === "stale" || dashboard.strategyState === "expired") && (
                <span className="manual-refresh-badge">待手动更新</span>
              )}
              <HelpTip label="解释策略时间">看板时间表示策略快照生成时点，不等于成交时点。</HelpTip>
              {(dashboard.strategyState === "stale" || dashboard.strategyState === "expired") && (
                <HelpTip label="解释手动更新状态">
                  这只是复核提醒，上一版策略仍保留在看板；只有你点击“更新最新策略”才会启动新的 Codex 取证。
                </HelpTip>
              )}
            </div>

            <div className="strategy-identity">
              <span className="direction-mark" aria-hidden="true">{directionArrow[strategy.direction]}</span>
              <div>
                <h1 id="strategy-title">
                  <span>{strategy.product}</span>
                  <b>{directionLabel(strategy.direction)}</b>
                </h1>
                <div className="contract-line">
                  <code>{strategy.contract}</code>
                  {partialBar ? (
                    <span className="bar-badge bar-badge--partial">
                      {bar.state === "partial_night" ? "夜盘收盘后部分日线" : "日盘进行中部分日线"}
                    </span>
                  ) : bar.state === "completed" ? (
                    <span className="bar-badge bar-badge--complete">完整日线</span>
                  ) : null}
                  {bar.state !== "unknown" && (
                    <HelpTip label="解释日线状态">
                      {partialBar
                        ? `状态=${bar.rawState}。${bar.state === "partial_night" ? "常规夜盘已收盘、日盘尚未完成" : "日盘仍在进行"}；仅可用于提前候选或条件触发，不能当作完成日线或确认突破。时点=${formatTimestamp(bar.asOf)}；覆盖=${bar.coverage || "未披露"}；行情来源=${bar.source || "未披露"}；交易日历=${bar.calendarEvidence || "未披露"}。`
                        : `状态=${bar.rawState}；时点=${formatTimestamp(bar.asOf)}；覆盖=${bar.coverage || "未披露"}；来源=${bar.source || "未披露"}。该日线已收盘，可用于完成态确认。`}
                    </HelpTip>
                  )}
                </div>
              </div>
            </div>

            {strategy.path && <p className="strategy-path">{strategy.path}</p>}
            {(strategy.currentPrice !== "—" || strategy.support !== "—" || strategy.resistance !== "—") && (
              <div className="market-anchors" aria-label="当前价格与关键位置">
                <span>现价 <b>{strategy.currentPrice}</b></span>
                <span>最强支撑 <b>{strategy.support}</b></span>
                <span>最强压力 <b>{strategy.resistance}</b></span>
                <HelpTip label="解释关键位置">每轮只保留一个最强支撑和一个最强压力；它们用于构造触发与失效，不构成自动交易指令。</HelpTip>
              </div>
            )}
            {strategy.whyNow && (
              <div className="why-now">
                <span>为什么是现在</span>
                <p>{strategy.whyNow}</p>
              </div>
            )}
          </div>

          <div className="strategy-levels" aria-label="交易结构">
            <Level label="触发" value={strategy.trigger} tone="signal" help="条件满足才进入候选；页面不会自动下单。" />
            <Level label="目标" value={strategy.target} tone="target" help="单一最强目标位或结构目标。" />
            <Level label="失效" value={strategy.invalidation} tone="danger" help="标的结构失效条件；它与期权成交价止损分开。" />
            <Level label="最晚兑现" value={formatTimestamp(strategy.deadline)} tone="muted" help="超过这一时点仍未兑现，应按时间失效重新评估。" />
          </div>
        </section>

        <section className="option-strip" id="option-panel" aria-labelledby="option-title">
          <div>
            <span className="eyebrow">买方期权</span>
            <h2 id="option-title">{optionActionLabel(strategy.optionAction)}</h2>
          </div>
          <div className="option-route">
            <strong>{strategy.optionRoute ? strategy.optionRoute.replace("long_", "LONG ").toUpperCase() : "暂不表达"}</strong>
            {strategy.optionContract && <code>{strategy.optionContract}</code>}
          </div>
          {strategy.optionReason ? (
            <HelpTip label="解释期权表达">{strategy.optionReason}</HelpTip>
          ) : (
            <HelpTip label="解释期权表达">方向判断与期权是否值得买入分开；报价、IV、Theta 或流动性不合算时会等待。</HelpTip>
          )}
        </section>

        <DecisionRail nodes={dashboard.nodes} activeId={activeNodeId} onNodeActivate={showLinkedPanel} />

        <section className="evidence-section" id="evidence-panel" aria-labelledby="evidence-title">
          <header className="section-heading">
            <div>
              <span className="eyebrow">决策底稿</span>
              <h2 id="evidence-title">证据状态</h2>
            </div>
            <HelpTip label="解释证据状态">可用性、新鲜度和完整性分别校验；缺口会进入不确定性或触发等待，不参与投票。</HelpTip>
          </header>

          <div className="evidence-list">
            {dashboard.evidence.length ? dashboard.evidence.map((item) => (
              <div className="evidence-row" key={item.domain}>
                <strong>{domainNames[item.domain] ?? item.domain}</strong>
                <span className={`evidence-state state-${item.availability}`}>{humanStatus(item.availability)}</span>
                <span className={`evidence-state state-${item.freshness}`}>{humanStatus(item.freshness)}</span>
                <span className={`evidence-state state-${item.integrity}`}>{humanStatus(item.integrity)}</span>
                {item.summary && <HelpTip label={`解释${domainNames[item.domain] ?? item.domain}`}>{item.summary}</HelpTip>}
              </div>
            )) : (
              <div className="evidence-empty">
                <span>本轮未返回证据明细</span>
                <HelpTip label="解释证据空状态">策略可显示，但无法从看板复核证据维度；下一次更新会重新拉取。</HelpTip>
              </div>
            )}
          </div>
        </section>

        {(strategy.counterevidence || strategy.falsifiers.length > 0) && (
          <section className="risk-line" id="risk-panel" aria-label="反证与失效条件">
            <strong>反证</strong>
            <span>{strategy.counterevidence || strategy.falsifiers[0]}</span>
            <HelpTip label="查看全部风险与可证伪条件">
              {strategy.falsifiers.length ? strategy.falsifiers.join("；") : "暂无额外可证伪条件。"}
            </HelpTip>
          </section>
        )}
        {!!dashboard.products.length && <MarketCatalog products={dashboard.products} />}
      </main>
    </div>
  );
}

interface HeaderProps {
  dashboard: DashboardView | null;
  isRunning: boolean;
  runMessage: string;
  product: string;
  onSelectProduct: (product: string) => void;
  onUpdate: () => void;
}

function WorkbenchHeader({ dashboard, isRunning, runMessage, product, onSelectProduct, onUpdate }: HeaderProps) {
  const connected = dashboard?.systemState === "healthy" || dashboard?.systemState === "ready";
  const codexNeedsLogin = dashboard?.codexState === "authentication_required";
  return (
    <header className="topbar">
      <div className="brand-lockup">
        <span className="brand-symbol" aria-hidden="true"><i /><i /><i /></span>
        <div>
          <strong>期策</strong>
          <span>LOCAL RESEARCH</span>
        </div>
      </div>

      <div className="runtime-line">
        <span className={`runtime-dot ${connected ? "is-connected" : ""}`} aria-hidden="true" />
        <span>{connected ? "本地服务在线" : "本地服务状态未知"}</span>
        {dashboard?.productCount !== null && dashboard?.productCount !== undefined && (
          <span>{dashboard.productCount} 个品种</span>
        )}
        <HelpTip label="解释系统状态">
          Codex 状态：{humanStatus(dashboard?.codexState ?? "unknown")}；运行阶段：{humanStatus(dashboard?.runPhase ?? "idle")}；生命周期：{humanStatus(dashboard?.lifecycle ?? "unstarted")}。
        </HelpTip>
      </div>

      <div className="research-actions">
        {!!dashboard?.products.length && (
          <select aria-label="研究范围" value={product} onChange={(event) => onSelectProduct(event.target.value)} disabled={isRunning}>
            <option value="">全市场</option>
            {dashboard.products.map((item) => (
              <option key={item.symbol} value={item.symbol}>{item.name} · {item.symbol}</option>
            ))}
          </select>
        )}
        <button
          className="primary-action topbar-action"
          type="button"
          onClick={onUpdate}
          disabled={isRunning || codexNeedsLogin}
          aria-label={isRunning ? runMessage || "正在更新策略" : "更新最新策略"}
          title={codexNeedsLogin ? "请先运行 codex login 并重新加载页面" : undefined}
        >
          <span className={isRunning ? "action-spinner" : "action-spark"} aria-hidden="true" />
          {isRunning ? "正在更新" : "更新最新策略"}
        </button>
      </div>
    </header>
  );
}

function Level({ label, value, tone, help }: { label: string; value: string; tone: string; help: string }) {
  return (
    <div className={`level level--${tone}`}>
      <div className="level-label">
        <span>{label}</span>
        <HelpTip label={`解释${label}`}>{help}</HelpTip>
      </div>
      <strong>{value}</strong>
    </div>
  );
}

export default App;
