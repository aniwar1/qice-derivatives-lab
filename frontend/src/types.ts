export type UnknownRecord = Record<string, unknown>;

export type NodeStatus =
  | "idle"
  | "planned"
  | "waiting"
  | "ready"
  | "in_progress"
  | "active"
  | "complete"
  | "blocked"
  | "optional"
  | "skipped";

export interface ArchitectureNode {
  id: string;
  label: string;
  owner: string;
  status: NodeStatus | string;
  summary: string;
}

export interface EvidenceView {
  domain: string;
  availability: string;
  freshness: string;
  integrity: string;
  observedAt: string | null;
  summary: string;
  facts: UnknownRecord;
}

export interface StrategyView {
  product: string;
  contract: string;
  direction: "long" | "short" | "neutral" | "uncertain" | "wait";
  path: string;
  trigger: string;
  target: string;
  invalidation: string;
  currentPrice: string;
  support: string;
  resistance: string;
  deadline: string | null;
  whyNow: string;
  counterevidence: string;
  falsifiers: string[];
  optionAction: string;
  optionRoute: string;
  optionContract: string;
  optionReason: string;
}

export interface BarView {
  state: "completed" | "partial_night" | "partial_day" | "unknown";
  rawState: string;
  asOf: string | null;
  coverage: string;
  source: string;
  calendarEvidence: string;
}

export interface DashboardView {
  generatedAt: string | null;
  strategy: StrategyView | null;
  nodes: ArchitectureNode[];
  edges: [string, string][];
  evidence: EvidenceView[];
  bar: BarView;
  runId: string | null;
  runPhase: string;
  lifecycle: string;
  productCount: number | null;
  products: { symbol: string; name: string; exchange: string }[];
  codexState: string;
  systemState: string;
  strategyState: string;
  strategyStateReason: string;
  raw: UnknownRecord;
}

export interface MarketQuote {
  product: string;
  price: number;
  observed_at: string;
  source_url: string;
}

export interface StrategyRunRequest {
  mode?: "all_market_scan" | "single_analysis" | "option_entry";
  product?: string;
  requested_contract?: string;
  option_analysis_requested?: boolean;
}

export interface StrategyRunAccepted {
  run_id: string;
  status: string;
  events_url: string;
}

export interface StrategyRunView {
  run_id: string;
  status: "queued" | "running" | "completed" | "failed" | string;
  result?: UnknownRecord | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at?: string;
  started_at?: string | null;
  completed_at?: string | null;
  updated_at?: string;
}

export interface RunEvent {
  event_id?: string | number;
  run_id?: string;
  type: "snapshot" | "progress" | "completed" | "failed" | "heartbeat" | string;
  stage?: string;
  message?: string;
  occurred_at?: string;
  payload?: unknown;
}
