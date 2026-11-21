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

export interface LiveAgentReview {
  sizing_basis?: string | null;
  technical: { assessment: string; source_ids: string[] };
  fundamentals: { assessment: string; source_ids: string[] };
  news: { assessment: string; source_ids: string[] };
  positioning: { classification: string; assessment: string };
  plans: Array<{ strategy: "A" | "B" | "C"; action: "wait" | "paper_entry"; reason: string; quantity: number; max_holding_days: number; legs: string[]; invalidation: string }>;
}

export interface ResearchDetail {
  run: StrategyRunView & {
    result?: { strategy: {
      agent_review?: LiveAgentReview | null;
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

export type AgentSession = {
  day: string; cutoff: string; context_sha256: string;
  decision: { product: string; action: string; technical: string; fundamentals: string; news: string; ownership: string;
    thesis: string; invalidation: string; max_entry_premium?: number; max_lots?: number;
    take_profit_close?: number; invalidation_price?: number; scenario_low?: number; scenario_high?: number; risk_bps: number; holding_days: number; legs: string[]; evidence_ids: string[] };
  execution: { status: string; reason: string; quantity?: number; raw_open_prices?: number[]; premium_and_fees?: number; effective_risk_bps?: number; budget_reason?: string; exits: Array<{ product: string; entry_day: string; pnl: number; reason: string }> };
};
type Plan = { id: "A" | "B" | "C"; stats: { return_pct: number | null; drawdown_pct: number | null; realized: number | null; open_pnl: number | null; opened: number; closed: number };
  curve: Array<{ day: string; equity: number; return_pct: number }>; sessions: AgentSession[] };
export type Study = { status: "missing"; plans: [] } | { status: "partial" | "complete"; start: string; end: string; model: string;
  completed_days: number; planned_days: number; evaluated_days: number; assumptions: string[]; plans: Plan[];
  context_coverage: Record<string, { technical: number; option_chain: number; fundamentals: number; news: number }> };

export interface FrozenContext {
  trade_date: string;
  daily_data_date: string;
  ownership: string;
  products: Record<string, {
    technical: null | { contract: string; close: number; volume: number; open_interest: number;
      change5_pct: number | null; realized_vol20_ann_pct: number | null };
    options: Array<{ id: string; premium: number; volume: number }>;
    monthly_aggregate: Record<string, { month: string; month_end_open_interest: number; volume_lots: number }>;
  }>;
  documents: Array<{ id: string; products: string[]; dimension: string; scope: string;
    title: string; excerpt: string; url: string; publisher: string; available_at: string; sha256?: string; freshness_role?: string }>;
}

export type ResearchMode = "baseline" | "primary" | "ranked";
type TermComparison = { paired_products: number; event_clusters: number; main_mean_return_pct: number | null; far_mean_return_pct: number | null; far_minus_main_pp: number | null };
type TermSummary = Record<"full"|"initial"|"holdout", TermComparison> & {one_day_decay?: {paired_products:number; main_pct_premium:number|null; far_pct_premium:number|null; main_pct_budget:number|null; far_pct_budget:number|null}};
export interface FrequencyResult {
  status: "complete" | "pending";
  completed_days: number;
  rows: Array<{ group: "A" | "B" | "C"; variant: ResearchMode; opened: number; open_days?: string[]; pnl: number; tail_pnl: number; drawdown_pct: number; curve: Array<{day:string; equity:number}> }>;
  per_group: Record<"A"|"B"|"C", ResearchMode>;
  control: Array<{id:string; opens:number; pnl:number; tail_pnl:number; drawdown_pct:number; mean_cost_pct:number}>;
  term: TermSummary | null;
  term_pairs: TermPair[];
  option_anchor?: {term: TermSummary; pairs: TermPair[]} | null;
  term_mode: "consider_far" | "compare_only";
}


export type FrequencySession = AgentSession & {
  rankings?: {primary: AgentSession["decision"]; reserve: AgentSession["decision"] | null};
  account_before?: {equity: number; cash: number; open_cost: number};
  screen?: Array<{rank: number; product?: string; reason: string; eligible: boolean}>;
  selected_rank?: number | null;
};
type PairSide = { contract: string; planned_quantity: number; legs: Array<{id: string}>; execution: {entry_cash: number; pnl_yuan: number} };
export type TermPair = {event_at: string; product: string; entry: string; exit: string; main: PairSide; far: PairSide};
