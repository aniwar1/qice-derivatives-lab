import type { StrategyGroup } from "../types";

export const COLORS = { A: "#235fb3", B: "#087d66", C: "#a65b23" };
export const names = {
  A: "多因子方向买权",
  B: "技术择强买权",
  C: "事件驱动宽跨式",
};
export const mandates = {
  A: "技术、基本面与消息面支持同一方向时，比较同月不同执行价；目标位需覆盖压力成本，单笔风险预算上限 0.5%。",
  B: "按实际合约的趋势、波动和流动性择强；基本面或消息证据不足时收紧预算，达到失效位或持有期限时退出。",
  C: "在已公布时间的事件前比较期权期限与双腿成本，核对两边盘口、事件后的到期余量和双向价格情景。",
};
export const currency = (value: number) =>
  new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
export const pct = (value: number) =>
  `${value > 0 ? "+" : ""}${value.toFixed(Math.abs(value) > 0 && Math.abs(value) < 0.005 ? 6 : 2)}%`;
export function PlanBadge({ id }: { id: StrategyGroup }) {
  return <span className={`plan-badge plan-${id.toLowerCase()}`}>{id}</span>;
}
