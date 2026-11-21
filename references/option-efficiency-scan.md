# 期权性价比候选扫描

`option_efficiency_scan` 先用期货数据筛选方向与潜在波动扩张，再对短名单核查真实期权。新开仓范围遵守 [direction-volatility-playbook.md](direction-volatility-playbook.md)：只找相对低 IV、方向明确、结构正在由压缩转向扩张的买方机会；新一轮整理后的突破需重新定价。用户只要求代理筛选时可止于观察候选；用户要求“找能做的机会”时，必须继续核查短名单 IV 与盘口，不能把代理排序作为最终性价比结论。

## 筛选逻辑

默认按持仓量识别实际主力。每个候选的结构、价量仓与真实期权链均绑定同一分析合约，指定月份按请求保留。

1. **方向边际**：初筛先选研究品种和月份，不先指定 call/put。对照最新价格结构、期限结构、基本面边际变化和会员资金，公平比较多、空与等待；须有独立证据解释所选方向为何在预计窗口占优。按 playbook 处理最强反证，不以“两类证据支持”机械盖过重大冲突；方向未定用 `two_sided_wait`。
2. **波动扩张边际**：最近 5–20 个交易日真实波动、日内振幅或区间宽度处于过去 40–60 个交易日的低位，或最新价刚从压缩区间边缘脱离；按 playbook 展示 HV 的样本与相邻窗口敏感性。期货实际波动收窄不能证明 IV 下降或即将上升。
3. **空间与催化**：结合剩余期限、正常波幅、第一结构障碍和真实期权成本判断空间是否足够。可验证的供需、政策、库存、资金行为或结构成熟证据应支持有期限的近期启动假设。只有长期供需方向或“横盘够久”，只能列为观察，不能称“即将爆发”。
4. **最新价覆盖层**：把最新价、最新成交量和最新持仓量作为独立的 `latest_price_overlay`。它可以更新“临界起爆/正在起爆/失败”的状态，不得改写最近已完成日 K 的 OHLC。
5. **真实期权核查**：对方向与启动时机成立的候选每批取 1–3 个同月链，有其他合理候选时继续核验；核对相对低 IV 的依据、报价时点、期限、偏斜和真实可成交深度，缺失时保持待核验。低 IV 已验证但没有近期启动证据的候选同样等待。

## 代理研究输出

以下字段描述研究短名单，不添加到 `option_decision.v1`。不可观测的边界如实标记。

- `analysis_contract`、`as_of`、`latest_price_overlay`
- `directional_edge`：`bullish`、`bearish` 或 `mixed`
- `volatility_expansion_edge`：`compressed`、`turning_up`、`already_expanding` 或 `unobservable`
- `compression_basis`、`catalyst`、`why_now`、`why_not_opposite`
- 一个 `entry_trigger`、一个 `futures_structure_invalidation`、一个 `latest_realization_time`
- `option_proxy_route`：`long_call_candidate`、`long_put_candidate` 或 `two_sided_wait`
- `opportunity_status`：`pre_expansion`、`expanding`、`already_expanded` 或 `passed`
- `pricing_gate`: 固定为 `real_option_chain_required_before_buy`

代理排序只能说明标的方向与实现波动扩张的候选依据。用户要求可做的机会时继续核查[四项准入](direction-volatility-playbook.md)；只有代理信息时，正式新开仓输出按[期权等待契约](long-option-entry.md#正式输出)处理。

有用户截图或行情更新时，先对齐月份、行权价、买卖侧和观察时间。较新的同条件真实报价可以更新或否决过时的候选。若标的大幅远离区间或权利金重估已消耗预期空间，标为 `already_expanded` 或 `passed`；若出现新的整理，重新核查该段的方向、启动与相对定价。旧截图、不同月份的 IV、理论价不能替代现时买入成本。

按证据独立性、相对 IV、剩余空间、Theta 与执行成本比较候选，批次不是机会数量上限。默认更新只交付合格方案，零合格时给等待和重评条件。
