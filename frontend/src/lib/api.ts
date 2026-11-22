import type { Study, FrozenContext, FrequencyResult, FrequencySession, ResearchMode } from "../types";
import type {
  RunEvent,
  ResearchDetail,
  ResearchReplay,
  ResearchRun,
  MarketQuote,
  ProductAnalysis,
  RollingBaseline, IntradayReference,
  StrategyRunAccepted,
  StrategyRunRequest,
  StrategyRunView,
  UnknownRecord,
} from "../types";

const rawBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim() ?? "";
const apiBase = rawBase.replace(/\/$/, "");

const endpoint = (path: string): string => `${apiBase}${path}`;

const readJson = async <T>(response: Response): Promise<T> => {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const value = body as UnknownRecord;
    const reason =
      (typeof value.detail === "string" && value.detail) ||
      (typeof value.message === "string" && value.message) ||
      `请求失败（${response.status}）`;
    throw new Error(reason);
  }
  return body as T;
};

export const getDashboard = async (signal?: AbortSignal): Promise<unknown> => {
  const response = await fetch(endpoint("/api/dashboard"), {
    headers: { Accept: "application/json" },
    signal,
  });
  return readJson(response);
};

export const getMarketQuote = async (product: string): Promise<MarketQuote> => {
  const response = await fetch(endpoint(`/api/market/quotes/${encodeURIComponent(product)}`));
  return readJson(response);
};

export const getLatestStrategy = async (signal?: AbortSignal): Promise<unknown> => {
  const response = await fetch(endpoint("/api/strategy/latest"), {
    headers: { Accept: "application/json" },
    signal,
  });
  return readJson(response);
};

export const createStrategyRun = async (
  request: StrategyRunRequest = { mode: "all_market_scan", option_analysis_requested: true },
): Promise<StrategyRunAccepted> => {
  const response = await fetch(endpoint("/api/strategy-runs"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
  });
  return readJson(response);
};

export const getStrategyRun = async (
  runId: string,
  signal?: AbortSignal,
): Promise<StrategyRunView> => {
  const response = await fetch(endpoint(`/api/strategy-runs/${encodeURIComponent(runId)}`), {
    headers: { Accept: "application/json" },
    signal,
  });
  return readJson(response);
};

const eventUrl = (url: string): string => {
  if (/^https?:\/\//i.test(url)) return url;
  return endpoint(url.startsWith("/") ? url : `/${url}`);
};

export const subscribeToRun = (
  accepted: StrategyRunAccepted,
  onEvent: (event: RunEvent) => void,
  onConnectionError: () => void,
): (() => void) => {
  const source = new EventSource(eventUrl(accepted.events_url));
  const consume = (message: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(message.data) as RunEvent);
    } catch {
      onEvent({ type: "progress", message: message.data });
    }
  };
  const eventNames = ["snapshot", "progress", "completed", "failed", "heartbeat"];
  source.onmessage = consume;
  eventNames.forEach((name) => source.addEventListener(name, consume as EventListener));
  source.onerror = onConnectionError;
  return () => source.close();
};

export const getResearchRuns = async (signal?: AbortSignal): Promise<ResearchRun[]> =>
  readJson(await fetch(endpoint("/api/research/runs"), { signal }));

export const getResearchDetail = async (runId: string, signal?: AbortSignal): Promise<ResearchDetail> =>
  readJson(await fetch(endpoint(`/api/research/runs/${encodeURIComponent(runId)}`), { signal }));

export const getResearchReplay = async (runId: string, at: string, signal?: AbortSignal): Promise<ResearchReplay> => {
  const query = at ? `?${new URLSearchParams({ at })}` : "";
  return readJson(await fetch(endpoint(`/api/research/runs/${encodeURIComponent(runId)}/replay${query}`), { signal }));
};

export const getRollingBaseline = async (signal?: AbortSignal): Promise<RollingBaseline> =>
  readJson(await fetch(endpoint("/api/workbench/rolling-baseline"), { signal, cache: "no-store" }));

export const getIntradayReference = async (signal?: AbortSignal): Promise<IntradayReference> =>
  readJson(await fetch(endpoint("/api/workbench/rolling-baseline/intraday"), { signal, cache: "no-store" }));

export const getProductAnalysis = async (product: string, signal?: AbortSignal): Promise<ProductAnalysis> =>
  readJson(await fetch(endpoint(`/api/workbench/products/${encodeURIComponent(product)}`), { signal }));

export const getAgentStudy = async (signal?: AbortSignal): Promise<Study> =>
  readJson(await fetch(endpoint("/api/workbench/agent-replay"), { signal }));

export const getFrozenContext = async (day: string, signal?: AbortSignal): Promise<FrozenContext> =>
  readJson(await fetch(endpoint(`/api/workbench/agent-replay/context/${encodeURIComponent(day)}`), { signal }));

export const getFrequencyStudy = async (signal?: AbortSignal): Promise<FrequencyResult> =>
  readJson(await fetch(endpoint("/api/workbench/frequency-study"), { signal }));

export const getFrequencySession = async (variant: ResearchMode, group: string, day: string, signal?: AbortSignal): Promise<FrequencySession> =>
  readJson(await fetch(endpoint(`/api/workbench/frequency-study/sessions/${variant}/${group}/${encodeURIComponent(day)}`), { signal }));
