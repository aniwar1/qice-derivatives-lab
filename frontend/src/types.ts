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

export interface ResearchRun {
  run_id: string;
  status: string;
  created_at: string;
  mode: string;
  product: string | null;
  decision: string | null;
}

export interface ResearchObservation {
  product: string;
  contract: string;
  observed_at: string;
  available_at: string;
  price: number;
  source: string;
  artifact_sha256: string;
  daily_status: string | null;
  daily_bars: Array<{
    date: string; open: number | null; high: number | null; low: number | null; close: number | null;
  }>;
}

export interface ResearchOpportunity {
  product: string;
  product_name: string | null;
  contract: string;
  option_symbol: string;
  decision: "long_call" | "long_put";
  entry_timing: string;
  thesis: string;
  why_now: string;
  counterevidence: string;
  trigger: number | null;
  invalidation: number | null;
  target: number | null;
  reassess_by: string;
  evidence_gaps: string[];
  option_sources: Array<{ url: string; title: string }>;
}

export interface ResearchDetail {
  run: StrategyRunView & {
    result?: { strategy: {
      decision: string;
      as_of: string;
      rationale: { thesis: string; strongest_counterevidence: string };
      evidence_gaps: string[];
    } } | null;
  };
  archive: {
    captured_at: string;
    expected_products: string[];
    observations: ResearchObservation[];
    gaps: Record<string, string>;
  } | null;
  archive_sha256: string | null;
  candidate_status: string;
  excluded_count: number;
  opportunities: ResearchOpportunity[];
  comparison: { previous_run_id: string; added: string[]; removed: string[]; changed: string[] } | null;
}

export interface ResearchReplay {
  review_as_of: string;
  evidence_as_of: string;
  validation: string;
  observations: ResearchObservation[];
  excluded_after_cutoff: number;
  followup: ResearchObservation[];
  followup_truncated: boolean;
  invalidation_observed: ResearchObservation | null;
}

export interface BacktestPoint {
  day: string;
  equity: number;
  return_pct: number;
  realized: number;
  open_pnl: number;
  open_positions: number;
}

export interface BacktestSession {
  day: string;
  information_day: string;
  outcome: {
    status: string;
    reason: string | null;
    product?: string;
    contract?: string;
    direction?: string | null;
    decision?: string;
    candidate_count?: number;
    quantity?: number;
    paid?: number;
    fee?: number;
    reference?: number;
    event?: { title: string; release_bjt: string; preannounced_source: string; transmission: string };
    legs?: Array<{ symbol: string; open_price: number }>;
  };
  exits: Array<{ trade_id: string; net_pnl: number; reason: string }>;
}

export interface StrategyPlan {
  id: "A" | "B" | "C";
  name: string;
  style: string;
  thesis: string;
  steps: [string, string][];
  limit: string;
  curve?: BacktestPoint[];
  sessions?: BacktestSession[];
  stats?: {
    opened: number; closed: number; open_positions: number; equity: number;
    return_pct: number; drawdown_pct: number; realized: number; open_pnl: number;
  };
}

export type WorkbenchBacktest =
  | { status: "missing"; batch: string; plans: StrategyPlan[] }
  | { status: "ready"; batch: string; plans: StrategyPlan[]; days: string[]; initial_cash: number;
      daily_cap: number; fee_per_side: number; covered_products: string[]; configured_count: number; market_sha256: string };

export interface ProductEvidence {
  status: string;
  freshness: string;
  conclusion: string;
  sources: Array<{ url: string; title: string; retrieved_at: string; locator: string }>;
}

export interface ProductAnalysis {
  product: string;
  observation: ResearchObservation | null;
  evidence_run_id: string | null;
  analysis: {
    run_id: string;
    result: {
      strategy: {
        analysis_contract: string | null;
        as_of: string;
        decision: string;
        reassess_by: string;
        evidence_gaps: string[];
        technical_structure: ProductEvidence;
        fundamentals_news: ProductEvidence;
        member_flow: ProductEvidence;
        option_market: ProductEvidence;
        rationale: { thesis: string; strongest_counterevidence: string };
        market_levels: { trigger?: number | null; target?: number | null; invalidation?: number | null };
        session_daily_bar: { bar_state: string; as_of: string } | null;
        quality_review: { strongest_challenge: string; resolution: string };
      };
    } | null;
  } | null;
}
