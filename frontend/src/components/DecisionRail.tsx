import { useMemo, useState } from "react";
import type { ArchitectureNode } from "../types";
import { HelpTip } from "./HelpTip";
import { humanStatus, nodeStage } from "../lib/model";

interface DecisionRailProps {
  nodes: ArchitectureNode[];
  activeId?: string | null;
  onNodeActivate?: (nodeId: string) => void;
}

const FALLBACK_NODES: ArchitectureNode[] = [
  ["intent_router", "识别任务"],
  ["scope_gate", "冻结边界"],
  ["market_scan", "全市场宽筛"],
  ["contract_binding", "绑定主力"],
  ["evidence_fanout", "并行取证"],
  ["evidence_validation", "证据校验"],
  ["agent_hypothesis", "主假设"],
  ["entry_path_comparison", "路径比较"],
  ["option_expression", "期权表达"],
  ["bounded_critic", "一次反证"],
  ["final_validation", "最终校验"],
  ["position_lifecycle", "持仓跟踪"],
  ["unified_dashboard", "看板投影"],
  ["learning_review", "真实复盘"],
].map(([id, label]) => ({ id, label, owner: "deterministic", status: "idle", summary: "" }));

const ownerLabel = (owner: string): string =>
  ({
    deterministic: "规则层",
    specialists: "证据专家",
    agent: "决策 Agent",
    state_machine: "状态机",
    renderer: "投影层",
  })[owner] ?? owner;

export function DecisionRail({ nodes, activeId, onNodeActivate }: DecisionRailProps) {
  const items = nodes.length ? nodes : FALLBACK_NODES;
  const firstActive = activeId ?? items.find((node) => ["active", "in_progress", "blocked"].includes(node.status))?.id;
  const [selectedId, setSelectedId] = useState<string | null>(firstActive ?? items[0]?.id ?? null);
  const selected = useMemo(
    () => items.find((node) => node.id === selectedId) ?? items[0],
    [items, selectedId],
  );

  return (
    <section className="rail-section" aria-labelledby="rail-title">
      <header className="section-heading">
        <div>
          <span className="eyebrow">运行架构</span>
          <h2 id="rail-title">脉冲决策轨道</h2>
        </div>
        <HelpTip label="解释脉冲决策轨道">
          节点显示本轮决策的实际运行位置。选择节点可查看职责；看板节点只读，不会改变决策。
        </HelpTip>
      </header>

      <nav className="decision-rail" aria-label="决策节点">
        {items.map((node, index) => (
          <button
            key={node.id}
            type="button"
            className={`rail-node status-${node.status} ${selected?.id === node.id ? "is-selected" : ""}`}
            onClick={() => {
              setSelectedId(node.id);
              onNodeActivate?.(node.id);
            }}
            aria-pressed={selected?.id === node.id}
          >
            <span className="rail-node__pulse" aria-hidden="true" />
            <span className="rail-node__index">{String(index + 1).padStart(2, "0")}</span>
            <span className="rail-node__label">{node.label}</span>
          </button>
        ))}
      </nav>

      {selected && (
        <div className="node-inspector" aria-live="polite">
          <div>
            <span className="node-inspector__stage">{nodeStage(selected.id)}</span>
            <strong>{selected.label}</strong>
          </div>
          <span className={`status-text status-text--${selected.status}`}>
            {humanStatus(selected.status)}
          </span>
          <span className="node-inspector__owner">{ownerLabel(selected.owner)}</span>
          {selected.summary && (
            <HelpTip label={`解释${selected.label}`}>{selected.summary}</HelpTip>
          )}
        </div>
      )}
    </section>
  );
}
