# EvidencePacket 与证据降级

每个取证分支返回一个 `evidence_packet.v1`，契约见[证据包 schema](../schemas/evidence_packet.v1.schema.json)。证据包是事实封套，不含方向或交易动作。

## 五轴状态

| 轴 | 状态 | 回答的问题 |
|---|---|---|
| `availability` | `complete`、`partial`、`unobservable` | 需要的事实是否齐全 |
| `freshness` | `fresh`、`stale`、`not_applicable` | 是否仍适用于当前判断时点 |
| `integrity` | `verified`、`degraded`、`unverified` | 保存字节、哈希与算术能否重放 |
| `identity` | `verified`、`degraded`、`unverified` | 品种、交易所、合约和来源身份是否闭合 |
| `executability` | `executable`、`incomplete`、`unobservable`、`not_applicable` | 相关动作是否有真实可成交条件 |

不要把多个轴压成一个含糊的 `status`。例如：旧行情可能 `availability=complete`、`integrity=verified`，但 `freshness=stale`；它可支持历史研究，却不能支持 `now` 委托。

## 必要字段

- `schema_version=evidence_packet.v1`、`run_id`、`packet_id`、`domain`；
- `product`、`analysis_contract`；
- `observed_at` 与必填但可为 null 的 `valid_until`；
- 五轴状态；
- 带 `retrieved_at`、定位符和内容哈希的 `source_refs`；
- 结构化 `facts` 与 `known_gaps`；
- 固定 `does_not_select_action=true`。

技术结构包可在 `facts.session_daily_bar` 携带 `session_aware_daily_bar.v1`。它必须把同一 `source` 同时列入 `source_refs`；若 `bar_state=session_partial`，证据 `availability` 必须保持 `partial`，并在 `known_gaps` 写入 `session_partial_daily_bar_not_completed`。bar 的 `product`、`analysis_contract`、`as_of`、交易日和品种时段必须与证据包闭合，任何未来覆盖失败关闭。

夜盘 partial 的时间、日历与确认门槛见[共用边界](invariants.md)。

证据包只追加到当前 `decision_run.v1`，不得由并行分支原地覆盖。相同 `packet_id` 拒绝重复；未来时间、跨品种或跨合约身份冲突失败关闭。

## 事实级时点与样本口径

- 为每个实际使用的价格、库存或会员事实分别绑定数据日期、合约/样本、来源与检索时点。页面顶部行情日期、抓取日期或搜索引擎“今天”不能替代正文数据日期；同页行情已更新而榜单仍旧时，分别标注，不升级旧榜新鲜度。
- 库存、现货、存栏等序列只在同一机构、地区、规格、环节与样本口径内比较。不同机构数值不一致时先核对原文和日期，再判断是旧闻误用、样本差异还是未解决冲突；保留各自序列，不拼接成趋势，不因两条不同数值就任意选有利一条。
- 最新完成交易日缺会员榜时，保留该日 `unobservable`，用前日已得榜说明历史背景；不把窗口向前挪一天后声称完成最近三日，也不把空表求和得到的0当作无增减。重点席位榜外只说明未观测，不能填零；Top20披露增减与固定席位相邻持仓差分别计算。
- 当日正式日线尚未返回时，保留有效完成历史及最新同合约报价，标明两者各自时点与质量；收盘后的报价也不自动成为已验证完整OHLC。不能伪造日线、确认突破或丢弃仍有效的历史结构；是否足以作持仓判断由已加载的三面门槛决定。
- 同月期权的bid/ask、最后成交、显示IV、涨跌幅和理论价分别核验。接口衍生涨跌与截图口径冲突时不污染一致的合约/报价字段；未核实涨跌基准就停用该衍生值。收盘深度不保证下一时段可成交。
- 缺Greeks时可用注明模型、利率、日计数、估值时点和提前行权局限的可复算情景辅助比较；模型校准IV不是软件实测IV或独立低估证据，未来IV是假设，不是预测。按方向×IV×时间比较，明确未计成本、价差与可执行性，不把理论值称为未来买一或已实现收益。
- 用户未提供真实开仓时间、实际手续费等字段时保留未知，金额只报可复算的未计费值或明确成本假设；不得为了通过正式schema补造时间或把未知费用填0。若当前schema无法表达这些未知，明确正式契约尚未通过，不发布当前可执行策略；这与可作有局限的研究判断分开。

正文提及关键数据却只能取得摘要时，标注摘要来源和核验局限，优先追到原文；关键冲突未解不得宣称已闭环。相同失败不反复重试：主源失败后尝试有独立价值的允许回退，记录失败与缺口，再判断结论是否受阻。

## 来源回退

每个 Adapter 在运行前声明：

```text
primary source → explicitly allowed fallback → degraded/unobservable
```

- 回退来源必须保留自己的真实身份，不能借用主来源的“官方”标签。
- 使用回退后更新 `integrity` 或 `identity`，并在 `known_gaps` 解释它如何影响结论。
- 找不到允许来源时写 `unobservable`；不得搜索到相似月份或相似品种后偷换。

## 硬阻塞与软缺口

下列问题阻止对应的“当前可执行”声明，但不替 Agent 决定市场方向：

- 品种不在当前 universe；
- 分析合约身份无法核验；
- 期权标的月份与分析合约不一致；
- 对实时决策使用晚于判断时点的事实；历史回放则按每个观察窗口截断当时可知信息，另留事后结果栏，见[历史复盘](historical-replay.md)；
- 需要执行却没有新鲜、非倒挂的 bid/ask；
- 期权到期早于预计兑现窗口。

其他缺口通常进入 Agent 的结果分布与不确定性判断。三面中一面 `partial` 并不自动等于 `wait`；但 Agent 必须说明该缺口为何仍不改变主假设，或据此选择结构化等待。
