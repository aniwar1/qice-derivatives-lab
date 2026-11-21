import type {
  ArchitectureNode,
  BarView,
  DashboardView,
  EvidenceView,
  StrategyView,
  UnknownRecord,
} from "../types";

const record = (value: unknown): UnknownRecord =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : {};

const array = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

const text = (...values: unknown[]): string => {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
};

const nullableText = (...values: unknown[]): string | null => text(...values) || null;

const statusAxis = (packet: UnknownRecord, axis: string): string => {
  const direct = packet[axis];
  if (typeof direct === "string") return direct;
  const axes = record(packet.status_axes ?? packet.status);
  const nested = axes[axis];
  if (typeof nested === "string") return nested;
  if (nested && typeof nested === "object") {
    return text(record(nested).status, record(nested).value) || "unknown";
  }
  return "unknown";
};

const stringifyLevel = (value: unknown): string => {
  if (typeof value === "string" || typeof value === "number") return String(value);
  const item = record(value);
  const price = text(item.price, item.level, item.value);
  const condition = text(item.condition, item.rule);
  const operators: Record<string, string> = {
    underlying_at_or_above: "≥",
    underlying_at_or_below: "≤",
    completed_close_at_or_above: "收盘 ≥",
    completed_close_at_or_below: "收盘 ≤",
  };
  const readableCondition = operators[condition] ?? condition.replaceAll("_", " ");
  return [readableCondition, price].filter(Boolean).join(" ");
};

const normalizeEvidence = (value: unknown): EvidenceView[] =>
  array(value).map((item, index) => {
    const packet = record(item);
    const facts = record(packet.facts);
    return {
      domain: text(packet.domain, packet.evidence_domain, packet.kind) || `evidence_${index + 1}`,
      availability: statusAxis(packet, "availability"),
      freshness: statusAxis(packet, "freshness"),
      integrity: statusAxis(packet, "integrity"),
      observedAt: nullableText(packet.observed_at, packet.as_of, facts.as_of),
      summary: text(packet.summary, packet.conclusion, packet.note),
      facts,
    };
  });

const latestPayload = (source: UnknownRecord): UnknownRecord => {
  const latest = record(source.latest_strategy);
  return record(latest.strategy ?? latest);
};

const evidenceFromLatest = (source: UnknownRecord): EvidenceView[] => {
  const payload = latestPayload(source);
  return ["technical_structure", "fundamentals_news", "member_flow", "option_market"]
    .map((domain) => {
      const item = record(payload[domain]);
      if (!Object.keys(item).length) return null;
      const sources = array(item.sources).map(record);
      return {
        domain,
        availability: text(item.status) || "unknown",
        freshness: text(item.freshness) || "unknown",
        integrity: "unknown",
        observedAt: nullableText(...sources.map((entry) => entry.retrieved_at)),
        summary: text(item.conclusion),
        facts: {},
      } satisfies EvidenceView;
    })
    .filter((item): item is EvidenceView => item !== null);
};

const findSessionBar = (evidence: EvidenceView[], source: UnknownRecord): BarView => {
  const technical = evidence.find((item) => item.domain === "technical_structure");
  const payload = latestPayload(source);
  const candidate = record(
    payload.session_daily_bar ??
      technical?.facts.session_daily_bar ??
      technical?.facts.daily_bar ??
      source.session_daily_bar ??
      source.bar_state,
  );
  const rawState = text(candidate.bar_state, candidate.state, candidate.status, candidate.completion);
  const normalized = rawState.toLowerCase();
  const coverage = record(candidate.coverage);
  const barSource = record(candidate.source);
  const calendarEvidence = record(candidate.calendar_evidence);
  const sessionKind = text(coverage.session_kind).toLowerCase();
  const state: BarView["state"] =
    normalized === "completed" || normalized === "complete" || normalized === "closed"
      ? "completed"
      : normalized.includes("night") || (normalized.includes("partial") && sessionKind === "night")
        ? "partial_night"
        : normalized.includes("day") || normalized.includes("partial") || normalized.includes("session")
          ? "partial_day"
          : "unknown";
  const coverageDetails = [
    text(coverage.session_kind) ? `会话=${text(coverage.session_kind)}` : "",
    text(coverage.schedule_group) ? `夜盘组=${text(coverage.schedule_group)}` : "",
    text(coverage.checkpoint_at) ? `检查点=${text(coverage.checkpoint_at)}` : "",
    text(coverage.observed_through) ? `观察至=${text(coverage.observed_through)}` : "",
    text(coverage.expected_daily_close_at) ? `日线收盘=${text(coverage.expected_daily_close_at)}` : "",
    text(coverage.covered_trading_seconds) ? `已覆盖秒=${text(coverage.covered_trading_seconds)}` : "",
  ].filter(Boolean);
  const sourceDetails = [
    text(barSource.kind) ? `类型=${text(barSource.kind)}` : "",
    text(barSource.locator) ? `定位=${text(barSource.locator)}` : "",
    text(barSource.retrieved_at) ? `抓取=${text(barSource.retrieved_at)}` : "",
    text(barSource.content_sha256) ? `SHA-256=${text(barSource.content_sha256)}` : "",
  ].filter(Boolean);
  const calendarDetails = [
    text(calendarEvidence.exchange) ? `交易所=${text(calendarEvidence.exchange)}` : "",
    text(calendarEvidence.trade_date) ? `交易日=${text(calendarEvidence.trade_date)}` : "",
    calendarEvidence.regular_session_confirmed === true ? "常规时段已确认" : "",
    text(calendarEvidence.url) ? `公告=${text(calendarEvidence.url)}` : "",
    text(calendarEvidence.retrieved_at) ? `抓取=${text(calendarEvidence.retrieved_at)}` : "",
    text(calendarEvidence.locator) ? `定位=${text(calendarEvidence.locator)}` : "",
  ].filter(Boolean);
  return {
    state,
    rawState: rawState || "unknown",
    asOf: nullableText(candidate.as_of, candidate.observed_at, technical?.observedAt),
    coverage: coverageDetails.join("；") || text(candidate.coverage_label, candidate.session_coverage),
    source: sourceDetails.join("；"),
    calendarEvidence: calendarDetails.join("；"),
  };
};

const normalizeStrategy = (root: UnknownRecord): StrategyView | null => {
  const latest = record(root.latest_strategy);
  const strategyPayload = record(latest.strategy ?? latest);
  const thesis = record(
    strategyPayload.underlying_thesis ??
      strategyPayload.thesis ??
      root.underlying_thesis ??
      strategyPayload,
  );
  const option = record(
    strategyPayload.option_expression ??
      root.option_expression ??
      strategyPayload.option_decision,
  );
  const optionFields = record(option.option_expression ?? option.expression ?? option);
  const selectedContract = record(record(option.option_market).selected_contract);
  const marketLevels = record(strategyPayload.market_levels ?? thesis.market_levels);
  const rationale = record(strategyPayload.rationale ?? thesis.rationale);
  const decision = text(strategyPayload.decision, latest.decision);

  const product = text(thesis.product, strategyPayload.product);
  const contract = text(
    thesis.analysis_contract,
    thesis.actual_main_contract,
    strategyPayload.analysis_contract,
    strategyPayload.actual_main_contract,
    strategyPayload.contract,
  );
  const direction = text(
    thesis.directional_view,
    thesis.underlying_action,
    strategyPayload.direction,
    strategyPayload.directional_view,
    strategyPayload.action,
  ).toLowerCase();

  if (!product && !contract && !direction) return null;

  const allowedDirections = new Set(["long", "short", "neutral", "uncertain", "wait"]);
  return {
    product: product || "—",
    contract: contract || "主力合约待绑定",
    direction: (allowedDirections.has(direction) ? direction : "uncertain") as StrategyView["direction"],
    path: text(thesis.path, strategyPayload.path, rationale.thesis, thesis.summary),
    trigger: stringifyLevel(thesis.trigger ?? strategyPayload.trigger ?? marketLevels.trigger) || "待更新",
    target: stringifyLevel(thesis.target ?? strategyPayload.target ?? marketLevels.target) || "待更新",
    invalidation:
      stringifyLevel(
        thesis.futures_structure_invalidation ??
          strategyPayload.futures_structure_invalidation ??
          strategyPayload.invalidation ??
          marketLevels.invalidation,
      ) || "待更新",
    currentPrice: stringifyLevel(marketLevels.current_price) || "—",
    support: stringifyLevel(marketLevels.strongest_support) || "—",
    resistance: stringifyLevel(marketLevels.strongest_resistance) || "—",
    deadline: nullableText(
      thesis.latest_realization_time,
      strategyPayload.latest_realization_time,
      strategyPayload.reassess_by,
    ),
    whyNow: text(thesis.why_now, strategyPayload.why_now, rationale.why_now),
    counterevidence: text(
      thesis.strongest_counterevidence,
      strategyPayload.strongest_counterevidence,
      strategyPayload.counterevidence,
      rationale.strongest_counterevidence,
    ),
    falsifiers: array(thesis.falsifiers ?? strategyPayload.falsifiers ?? strategyPayload.risks)
      .filter((item): item is string => typeof item === "string")
      .map((item) => item.trim())
      .filter(Boolean),
    optionAction: text(
      optionFields.capital_action,
      option.capital_action,
      option.action,
      strategyPayload.option_action,
      decision === "long_call" || decision === "long_put" ? "buy_to_open" : undefined,
      decision === "wait" ? "wait" : undefined,
      decision === "futures" ? "not_selected" : undefined,
    ) || "wait",
    optionRoute: text(optionFields.option_route, optionFields.route, option.option_route, strategyPayload.option_route),
    optionContract: text(selectedContract.symbol, optionFields.symbol, optionFields.contract, strategyPayload.option_contract),
    optionReason: text(
      record(option.rationale).summary,
      record(option.rationale).decision,
      option.reason,
      strategyPayload.option_reason,
      record(strategyPayload.option_market).conclusion,
    ),
  };
};

const normalizeNodes = (root: UnknownRecord): ArchitectureNode[] => {
  const architecture = record(root.architecture);
  return array(architecture.nodes).map((item) => {
    const node = record(item);
    return {
      id: text(node.id),
      label: text(node.label, node.name, node.id),
      owner: text(node.owner) || "deterministic",
      status: text(node.status) || "idle",
      summary: text(node.summary, node.description),
    };
  }).filter((node) => node.id);
};

const normalizeEdges = (root: UnknownRecord): [string, string][] => {
  const architecture = record(root.architecture);
  return array(architecture.edges)
    .filter((item): item is unknown[] => Array.isArray(item) && item.length === 2)
    .map((item) => [text(item[0]), text(item[1])])
    .filter((item): item is [string, string] => Boolean(item[0] && item[1]));
};

export const normalizeDashboard = (input: unknown): DashboardView => {
  const envelope = record(input);
  const domainSnapshot = record(envelope.domain_snapshot);
  const root: UnknownRecord = { ...domainSnapshot, ...envelope };
  const projectedEvidence = normalizeEvidence(root.evidence);
  const evidence = projectedEvidence.length ? projectedEvidence : evidenceFromLatest(root);
  const currentRun = record(root.current_run ?? root.active_run);
  const lifecycle = record(root.lifecycle);
  const universe = record(root.universe);
  const codex = record(root.codex);
  const system = record(root.system);
  const strategyStatus = record(root.latest_strategy_status);

  return {
    generatedAt: nullableText(root.generated_at, root.updated_at),
    strategy: normalizeStrategy(root),
    nodes: normalizeNodes(root),
    edges: normalizeEdges(root),
    evidence,
    bar: findSessionBar(evidence, root),
    runId: nullableText(currentRun.run_id, currentRun.id),
    runPhase: text(currentRun.phase, currentRun.status) || "idle",
    lifecycle: text(lifecycle.current_state, root.lifecycle_state) || "unstarted",
    productCount:
      typeof universe.product_count === "number"
        ? universe.product_count
        : Array.isArray(universe.products)
          ? universe.products.length
          : null,
    products: Array.isArray(root.product_catalog)
      ? root.product_catalog.map(record).flatMap((item) => {
          const symbol = text(item.symbol);
          const name = text(item.name);
          const exchange = text(item.exchange);
          return symbol && name && exchange ? [{ symbol, name, exchange }] : [];
        })
      : [],
    codexState:
      text(codex.status, codex.state, root.codex_status) ||
      (codex.authenticated === true ? "ready" : codex.authenticated === false ? "blocked" : "unknown"),
    systemState: text(system.status, system.state, root.system_status) || "unknown",
    strategyState: text(strategyStatus.state) || (normalizeStrategy(root) ? "current" : "missing"),
    strategyStateReason: text(strategyStatus.reason),
    raw: root,
  };
};

export const formatTimestamp = (value: string | null): string => {
  if (!value) return "时间待更新";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  }).format(date);
};

export const directionLabel = (direction: StrategyView["direction"]): string =>
  ({
    long: "偏多",
    short: "偏空",
    neutral: "中性",
    uncertain: "证据不足",
    wait: "等待",
  })[direction];

export const humanStatus = (status: string): string => {
  const labels: Record<string, string> = {
    idle: "待命",
    planned: "已规划",
    waiting: "等待证据",
    ready: "可执行",
    authentication_required: "待登录",
    in_progress: "运行中",
    active: "运行中",
    complete: "已完成",
    completed: "已完成",
    blocked: "已阻断",
    optional: "按需",
    skipped: "已跳过",
    fresh: "新鲜",
    stale: "待手动更新",
    verified: "已核验",
    degraded: "降级",
    partial: "部分可用",
    unobservable: "不可观测",
    unknown: "未报告",
    finalized: "已定稿",
    collecting_evidence: "收集证据",
    ready_for_agent: "等待决策",
    agent_decided: "决策完成",
    quality_review: "反证复核",
  };
  return labels[status] ?? status.replaceAll("_", " ");
};

export const nodeStage = (id: string): string => {
  const stages: Record<string, string> = {
    intent_router: "路由",
    scope_gate: "路由",
    market_scan: "冻结",
    contract_binding: "冻结",
    evidence_fanout: "证据",
    evidence_validation: "证据",
    agent_hypothesis: "决策",
    entry_path_comparison: "决策",
    option_expression: "决策",
    bounded_critic: "复核",
    final_validation: "复核",
    position_lifecycle: "跟踪",
    unified_dashboard: "投影",
    learning_review: "学习",
  };
  return stages[id] ?? "运行";
};
