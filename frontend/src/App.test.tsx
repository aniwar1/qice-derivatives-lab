import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import App from "./App";

const decision = {
  day: "2026-11-24", cutoff: "2026-11-24T18:00:00+08:00", status: "观察", product: "",
  legs: [], thesis: "等待开仓条件", technical: "", fundamentals: "", news: "", invalidation: "", evidence: [],
};
const report = {
  start: "2026-07-01", end: "2026-11-24", as_of: "2026-11-24", initial_capital: 200000, trade_budget: 10000, per_leg_budget: 5000,
  method: "按收盘估值", issues: [], strategies: ["A","B","C"].map((id,i) => ({
    id, name: "测试策略" + id, version: "测试版本", rules: ["测试执行规则" + id],
    policy_id: id === "B" ? "short_rotation" : "opening",
    validation: id === "B" ? {start:"2026-10-01",end:"2026-11-24",baseline:{opened:3,net_pnl:-900},selected:{opened:5,net_pnl:150}} : null,
    decision: id === "B" ? {...decision, order_source:"A",selection_reason:"首选等待，备选满足趋势要求",
      effective_order: {...decision,status:"计划开仓",product:"JD",legs:["JD2610C3300"],thesis:"执行备选信号"},
      execution:{status:"simulated_open", reason:"日线代理", fill_method:"daily_limit_proxy",
        market_open_prices:[70], observed_daily_low:61,filled_premium_including_slippage:65}} : decision,
    fills: id === "C" ? [
      {day:"2026-09-08",product:"JD",premium_and_fees:9776,leg_fills:[
        {leg:"JD2610C3300",quantity:6,premium_and_fees:4812},{leg:"JD2610P3100",quantity:8,premium_and_fees:4964}]},
      {day:"2026-09-01",product:"SR",premium_and_fees:9600,leg_fills:[
        {leg:"SR2701C5300",quantity:5,premium_and_fees:4800},{leg:"SR2701P5100",quantity:4,premium_and_fees:4800}]},
    ] : [],
    curve: [{day:"2026-07-01",equity:200000,return_pct:0},{day:"2026-11-24",equity:200000 + 2000*i,return_pct:i}],
  })),
};
const products = [{symbol:"JD",name:"鸡蛋",exchange:"DCE"},{symbol:"M",name:"豆粕",exchange:"DCE"}];
const history = (product: typeof products[number]) => ({
  ...product, product: product.symbol, records: ["2026-11-24","2026-11-23"].map(day => ({
    day, at:null, kind:"研究记录", title: product.name + day, sections:[{label:"基本面",text:"库存分析"}],
    sources:[{title:"交易所",url:"https://example.com/"}],
  })),
});
const fetcher = vi.fn(async (url: string) => {
  const product = products.find(item => url.endsWith("/history/" + item.symbol));
  const data = url.endsWith("/strategies") ? report : product ? history(product) : {products};
  return new Response(JSON.stringify(data), {status:200,headers:{"Content-Type":"application/json"}});
});

beforeEach(() => { fetcher.mockClear(); vi.stubGlobal("fetch",fetcher); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("两页研究工作台", () => {
  it("旧页面地址直接回到三策略曲线并读取最新预算和决策", async () => {
    window.history.replaceState(null,"","#frequency");
    render(<App/>);
    expect(await screen.findByText("¥ 10,000")).toBeVisible();
    expect(screen.getByText("¥ 5,000")).toBeVisible();
    expect(screen.getByText(/有效委托来自策略 A/)).toBeVisible();
    expect(screen.getByText(/模型原判 · 观察/)).toBeVisible();
    expect(screen.getByText(/日内最低 61/)).toBeVisible();
    expect(screen.getByText(/扣费净损益由 -900 元变为 150 元/)).toBeVisible();
    let fills = within(screen.getByRole("table", {name:"各腿开仓成交"}));
    expect(within(fills.getByRole("row",{name:/JD2610C3300/})).getByText("6")).toBeVisible();
    expect(within(fills.getByRole("row",{name:/JD2610P3100/})).getByText("4,964.00")).toBeVisible();
    fireEvent.change(screen.getByLabelText("开仓日期"), {target:{value:"2026-09-01"}});
    fills = within(screen.getByRole("table", {name:"各腿开仓成交"}));
    expect(within(fills.getByRole("row",{name:/SR2701P5100/})).getByText("4")).toBeVisible();
    expect(screen.getByRole("link", {name:"查看该日品种分析与成交记录"})).toHaveAttribute("href","#history/SR/2026-09-01");
    expect(window.location.hash).toBe("#strategies");
    expect(within(screen.getByRole("navigation")).getAllByRole("link")).toHaveLength(2);
    expect(screen.getAllByRole("heading", {name:"当天决策"})).toHaveLength(3);
    expect(screen.getByText("测试执行规则C")).toBeVisible();
    expect(screen.getByRole("figure",{name:"三策略逐日累计收益率"})).toBeVisible();
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(["/api/workbench/strategies"]);
  });
  it("按 URL 选择品种，切换查看历史日的完整分析与来源", async () => {
    window.history.replaceState(null,"","#history/M/2026-11-23");
    render(<App/>);
    expect(await screen.findByRole("heading",{name:"豆粕2026-11-23"})).toBeVisible();
    fireEvent.click(screen.getByRole("button",{name:"后一日"}));
    expect(screen.getByRole("heading",{name:"豆粕2026-11-24"})).toBeVisible();
    expect(window.location.hash).toBe("#history/M/2026-11-24");
    expect(screen.getByText("库存分析")).toBeVisible();
    expect(screen.getByRole("link",{name:"交易所"})).toHaveAttribute("href","https://example.com/");
    fireEvent.change(screen.getByRole("searchbox"), {target:{value:"JD"}});
    const results = within(screen.getByRole("list",{name:"品种列表"}));
    expect(results.getAllByRole("link")).toHaveLength(1);
    expect(results.getByRole("link")).toHaveAttribute("href","#history/JD");
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(["/api/workbench/history","/api/workbench/history/M"]);
  });
});
