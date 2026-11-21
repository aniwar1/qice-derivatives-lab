# 决策与证据边界

## 决策权与账户

专家分支只提交各自的证据包。决策 Agent 比较竞争解释并负责最终观点。脚本校验来源、身份、时点、算术和内部一致性。不得用投票、评分或通过校验代替方向判断。报告、验证和提醒均不授权下单或卖出开仓期权。

品种范围以当前[配置](../config/trading_universe.json)为准，正式研究先检查范围，再请求行情。默认“更新”覆盖范围内品种，推送全部合格机会。账户资金、旧仓和手数不参与筛选。按报价单位与每手展示一次标准化入场和全平情景，用户决定手数、分仓和加仓。自行增减仓不能延后原定的价格或时间失效条件。

## 合约与时点

- 默认扫描按同批持仓量识别实际主力，披露链覆盖、并列与移仓情况。先绑定 `analysis_contract` 再取月份相关证据。指定月份、已持期权和历史复盘均以请求所指的实际交割合约为准；今日主力不能替换当时标的。期权链、价位和会员数据须与对应分析月份一致。
- 用完成的历史 K 线建立结构和确认枢轴。只有 `completed` 日线可以支持 `confirmed_daily_breakout`。分钟结构须有相应频率的证据，只能作该周期的判断。
- 夜盘收盘后的 `session_partial` 日线仅可叠加于完成历史作 `anticipatory|conditional` 研究。按品种的常规夜盘结束时间（北京时间 23:00、次日 01:00 或 02:30）检查，披露 `bar_state`、`as_of`、覆盖、来源；交易日和品种须与 `calendar_evidence` 匹配。日历来源为该交易所公开官方 HTTPS，检索不晚于 bar 且距其不超过 24 小时，须确认常规时段未变。缺失、过期、跨交易所或时段存疑，拒用 partial；无夜盘品种不适用此豁免。完整覆盖至 15:00 的 `completed` bar 使用 `calendar_evidence=null`。[日线契约](../schemas/session_aware_daily_bar.v1.schema.json)
- 对不可观测行情、成本、IV、Greeks、成交及资金身份说明缺口与影响。概率数字须有明确事件、期限、可复算方法及局限；区分观察、估值与未来情景。`verified` 只表示指定证据可复核。

## 新开仓与已有持仓

- `underlying_thesis.v1` 记录标的方向、路径、触发、目标、结构失效及期限。`option_decision.v1` 另记期权路线与资金动作。新开仓须满足[方向、启动、相对定价与成本](direction-volatility-playbook.md)，具体申报与参数以[期权契约](long-option-entry.md)为准。
- 新机会证据不足为 `wait`，说明等待什么、何时重评及何种变化触发重评。v3 的结构化等待使用 `wait_for`、`reassess_by`、`update_triggers`；期权契约的 `reassessment` 只接受 `reassess_by` 和 `update_triggers`。
- 已有买方持仓按[持仓门槛](decision-loop.md#三面闭环与持仓结论门槛)给出 `hold|close`，其中 `close` 只表示平掉原持仓。关键证据缺失且无法判断时，报告复核未完成。用户已确认的原止损优先。
- 分开记录实时 `option_execution_stop`、完成 K 线的 `futures_structure_invalidation` 与 `latest_realization_time`。按可成交 ask 估算买入、按 bid 估算退出，并核对深度与期限。实际费率未知时披露公开费用、增量成本假设及其敏感性。
- 正式机会判断先校验证据，再进行一次独立反证，最多修订一次。重大冲突未解时剔除新机会；持仓复核按已获证据交代局限与重评条件。

## 学习

保留最终证据、结论和简短归因。经用户确认、可复核的真实成交与净盈亏才进入实盘学习。[历史行情案例](historical-replay.md)只沉淀取证方法和待验证假设；[前向模拟](forward-paper-evolution.md)独立冻结计划、记录假设和样本分母。交易规则的晋升和授权见[自进化](self-evolution.md)。采集脚本不自行改写 Skill。
