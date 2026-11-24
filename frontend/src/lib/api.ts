import type { Product, ProductHistory, StrategyReport } from "../types";

const base = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/$/, "");

async function get<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(`${base}/api/workbench/${path}`, {
    signal, cache: "no-store", headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : `读取数据失败（${response.status}）`);
  }
  return response.json();
}

export const getStrategies = (signal: AbortSignal) => get<StrategyReport>("strategies", signal);
export const getProducts = (signal: AbortSignal) => get<{products: Product[]}>("history", signal);
export const getHistory = (product: string, signal: AbortSignal) =>
  get<ProductHistory>(`history/${encodeURIComponent(product)}`, signal);
