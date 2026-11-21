import { useCallback, useEffect, useRef, useState } from "react";
import { FrequencyStudy } from "./components/FrequencyStudy";
import { AgentStudy } from "./components/AgentStudy";
import { AsyncState } from "./components/AsyncState";
import { DecisionRail } from "./components/DecisionRail";
import { HelpTip } from "./components/HelpTip";
import { ProductWorkbench } from "./components/ProductWorkbench";
import { ResearchPanel } from "./components/ResearchPanel";
import { StrategyOverview, StrategyPlans } from "./components/StrategyWorkbench";
import { createStrategyRun, getDashboard, getStrategyRun, getWorkbenchBacktest, subscribeToRun } from "./lib/api";
import { directionLabel, formatTimestamp, humanStatus, normalizeDashboard } from "./lib/model";
import type { DashboardView, RunEvent, StrategyPlan, StrategyRunAccepted, WorkbenchBacktest } from "./types";

type LoadState = "loading" | "ready" | "error";
type Route = { page: "frequency" | "agent" | "overview" | "plans" | "products" | "archive"; id?: string; day?: string };

const pages = [
  { page: "frequency", label: "频率与期限对照" },
  { page: "agent", label: "Agent 策略回测" },
  { page: "overview", label: "十月回测基线" },
  { page: "plans", label: "十月基线计划" },
  { page: "products", label: "品种研究" },
  { page: "archive", label: "研究档案" },
] as const;

function readRoute(): Route {
  const [page, id, day] = window.location.hash.slice(1).split("/");
  if (page === "plans") return { page, id: /^[ABC]$/.test(id ?? "") ? id : "A", day: /^\d{4}-\d{2}-\d{2}$/.test(day ?? "") ? day : undefined };
  if (page === "products" || page === "archive") {
    return { page, id: /^[A-Za-z0-9_-]+$/.test(id ?? "") ? id : undefined };
  }
  return { page: page === "overview" || page === "agent" ? page : "frequency" };
}

function App() {
  const [route, setRoute] = useState(readRoute);
  const [backtest, setBacktest] = useState<WorkbenchBacktest | null>(null);
  const [backtestError, setBacktestError] = useState("");

  useEffect(() => {
    const change = () => {
      setRoute(readRoute());
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void getWorkbenchBacktest(controller.signal).then(setBacktest).catch((reason: Error) => {
      if (!controller.signal.aborted) setBacktestError(reason.message || "回测载入失败。");
    });
    return () => controller.abort();
  }, []);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [dashboard, setDashboard] = useState<DashboardView | null>(null);
  const [error, setError] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [runMessage, setRunMessage] = useState("");
  const [finishedRunId, setFinishedRunId] = useState<string | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [researchRevision, setResearchRevision] = useState(0);
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
      setResearchRevision((value) => value + 1);
      if (event.type === "failed") {
        setError(event.message || "策略更新失败，请检查证据来源后重试。");
        setRunMessage("");
        setIsRunning(false);
        return;
      }
      setFinishedRunId(runId ?? null);
      setRunMessage("策略已更新");
      setIsRunning(false);
      await load();
    },
    [load],
  );

  const startRun = useCallback(async (product = "") => {
    if (isRunning) return;
    setError("");
    setIsRunning(true);
    setRunMessage("正在提交手动更新");
    setFinishedRunId(null);
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
  }, [dashboard, isRunning]);

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


  const openPlan = (id: StrategyPlan["id"], day?: string) => { window.location.hash = `plans/${id}${day ? `/${day}` : ""}`; };
  const openProduct = (symbol: string) => { window.location.hash = `products/${symbol}`; };
  const openArchive = (runId: string) => { window.location.hash = `archive/${runId}`; };
  const canRun = !!dashboard && !isRunning && !activeRunId && dashboard.codexState !== "authentication_required";
  const products = dashboard?.products ?? [];
  const symbol = products.find((item) => item.symbol === route.id)?.symbol
    ?? products.find((item) => item.symbol === "JD")?.symbol ?? products[0]?.symbol;
  const connected = dashboard?.systemState === "healthy" || dashboard?.systemState === "ready";
  const latest = dashboard?.strategy;
  const activeNodeId = dashboard?.nodes.find((node) => ["active", "in_progress", "blocked"].includes(node.status))?.id;

  return (
    <div className="workspace-shell">
      <a className="skip-link" href="#workspace-main" onClick={(event) => {
        event.preventDefault();
        document.getElementById("workspace-main")?.focus();
      }}>跳到主内容</a>
      <aside className="workspace-sidebar">
        <a href="#agent" className="workspace-brand"><span className="brand-monogram" aria-hidden="true">期</span><span><strong>期策</strong><small>衍生品研究工作台</small></span></a>
        <nav aria-label="工作台导航">{pages.map((item) => (
          <a key={item.page} href={`#${item.page}`} aria-current={route.page === item.page ? "page" : undefined}>{item.label}</a>
        ))}</nav>
        <div className="workspace-connection"><i className={connected ? "connected" : ""} aria-hidden="true" />{connected ? "本地服务在线" : "本地服务状态未知"}</div>
      </aside>
      <div className="workspace-main">
        <header className="workspace-toolbar">
          <span>{products.length ? `中国商品期货 · ${products.length} 个研究品种` : "中国商品期货"}</span>
          <button className="primary-action" type="button" disabled={!canRun} onClick={() => void startRun()}>运行全市场研究</button>
        </header>
        <main id="workspace-main" className="workspace-content" tabIndex={-1}>
          {error && <div className="alert workspace-alert" role="alert"><span>{error}</span><button className="text-action" onClick={() => void load()}>重新连接</button></div>}
          {dashboard?.codexState === "authentication_required" && <p className="alert">启动研究前，请在终端运行 <code>codex login</code>，然后重新加载页面。</p>}
          {runMessage && <div className="workspace-run" role="status"><span>{runMessage}</span>{finishedRunId ? <button className="text-action" onClick={() => openArchive(finishedRunId)}>查看本次研究 →</button> : <HelpTip label="解释研究进度">实时连接中断时，工作台会继续查询本地服务中的任务状态。</HelpTip>}</div>}
          {route.page === "frequency" && <FrequencyStudy />}
          {route.page === "agent" && <AgentStudy namesByProduct={Object.fromEntries(products.map(p => [p.symbol, p.name]))} />}
          {route.page === "overview" && <StrategyOverview data={backtest} error={backtestError} openPlan={openPlan} />}
          {route.page === "plans" && <>
            {backtestError && <p className="alert" role="alert">{backtestError}</p>}
            <StrategyPlans key={`${route.id}/${route.day}`} data={backtest} id={route.id as StrategyPlan["id"]} initialDay={route.day} onSelect={openPlan} openProduct={openProduct} />
          </>}
          {route.page === "products" && (symbol
            ? <ProductWorkbench products={products} symbol={symbol} choose={openProduct} openPlan={openPlan} openArchive={openArchive} backtest={backtest} revision={researchRevision} canRun={canRun} startResearch={(target) => void startRun(target)} />
            : <AsyncState kind={loadState === "loading" ? "loading" : "empty"} title={loadState === "loading" ? "正在加载品种" : "暂无可研究品种"} />)}
          {route.page === "archive" && <>
            <header className="page-heading"><div><h1>研究档案</h1><p>查看历史研究、原始证据和指定时点的回放。</p></div></header>
            {latest && <section className="surface plan-detail latest-summary">
              <div className="section-heading"><div><h2>最近归档的研究判断</h2><p>{formatTimestamp(dashboard?.generatedAt)} · {latest.contract === "—" ? "全市场" : latest.contract}</p></div>
                <span className="tag">{directionLabel(latest.direction)} · {humanStatus(dashboard?.strategyState ?? "unknown")}</span></div>
              {latest.path && <p>{latest.path}</p>}{latest.whyNow && <p>{latest.whyNow}</p>}
            </section>}
            <ResearchPanel key={route.id ?? "archive"} revision={researchRevision} initialRunId={route.id} />
            {!!dashboard?.nodes.length && <details className="surface archive-process"><summary>查看研究流程</summary><DecisionRail nodes={dashboard.nodes} activeId={activeNodeId} /></details>}
          </>}
        </main>
      </div>
    </div>
  );
}

export default App;
