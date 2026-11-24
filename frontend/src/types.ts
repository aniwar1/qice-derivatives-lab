export interface LegFill {
  leg: string;
  quantity: number;
  requested_budget_cny?: number;
  budget_cny?: number;
  premium_and_fees?: number;
  proceeds_after_costs?: number;
  pnl?: number;
}
export interface TradeFill {
  day: string;
  product: string;
  premium_and_fees: number;
  leg_fills: LegFill[];
}
export interface DecisionPlan {
  day: string;
  cutoff: string;
  status: string;
  product: string;
  legs: string[];
  thesis: string;
  technical: string;
  fundamentals: string;
  news: string;
  invalidation: string;
  evidence: string[];
  mode?: string;
  ownership?: string;
  holding_days?: number;
  invalidation_price?: number | null;
  take_profit_close?: number | null;
  max_entry_premium?: number | null;
  max_lots?: number | null;
  requested_budget_cny?: number;
  scenario_low?: number | null;
  scenario_high?: number | null;
  scenario_target?: number | null;
}
export interface StrategyDecision extends DecisionPlan {
  effective_order?: DecisionPlan | null;
  order_source?: string;
  selection_reason?: string;
  execution?: {
    status: string; reason: string; quantity?: number; premium_and_fees?: number;
    fill_method?: string; observed_daily_low?: number; filled_premium_including_slippage?: number;
    market_open_prices?: number[]; raw_entry_prices?: number[];
    leg_fills?: LegFill[];
    exits?: {product: string; quantity?: number; pnl: number; reason: string; leg_fills?: LegFill[]}[];
  } | null;
}
export interface Strategy {
  id: "A" | "B" | "C";
  name: string;
  version: string;
  rules: string[];
  policy_id?: string;
  validation?: {
    start: string; end: string;
    baseline: {opened:number; net_pnl:number};
    selected: {opened:number; net_pnl:number};
  } | null;
  curve: Array<{ day: string; equity: number | null; return_pct: number | null }>;
  decision: StrategyDecision;
  opened: number;
  closed: number;
  fills?: TradeFill[];
}
export interface StrategyReport {
  start: string;
  end: string | null;
  as_of: string;
  initial_capital: number;
  trade_budget: number;
  per_leg_budget: number;
  method: string;
  issues: string[];
  strategies: Strategy[];
}
export interface Product {
  symbol: string;
  name: string;
  exchange: string;
}
export interface HistoryRecord {
  day: string;
  at: string | null;
  kind: string;
  title: string;
  sections: { label: string; text: string }[];
  sources: { title: string; url: string }[];
}
export interface ProductHistory {
  product: string;
  name: string;
  exchange: string;
  records: HistoryRecord[];
}
