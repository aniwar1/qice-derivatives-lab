# Skill 自进化与真实交易学习

## 流程自进化

若一次实际使用暴露了取证、时点、样本、路由或结论顺序错误，可依据已有授权立即修复方法。未结束的真实持仓也能暴露流程错误，其盈亏分类须等证据齐全。用户明确改变研究范围时按新偏好调整入口，策略有效性另行验证。

1. 记录可复核的输入、来源、时间、错误结论及其影响。纠正不受支持的结论，继续用户原先的研究任务。
2. 找到唯一负责该判断的指引，做最小修复。历史行情里出现的新形态留作研究候选，事后挑选的窗口或阈值不能直接升级为通用交易规则。
3. 用会失败的情形和仍应放行的正例，在独立上下文实际运行，核查事实、理由及最终行为。检查格式、引用和改动范围，最多按评测结果修订一次。未解决的实质问题保留为未通过。
4. 按当前授权同步安装入口和文档源，提交精确文件并回报验证和版本。安装入口为符号链接时先核对目标，维护同一份正文。

| 请求或证据 | 产物归属 |
| --- | --- |
| 回看历史行情、比较起爆与假突破 | [历史行情复盘](historical-replay.md)生成研究文档，历史观察不进入模拟或实盘账本。 |
| 有预登记的前向模拟计划 | [前向模拟复盘](forward-paper-evolution.md)按冻结版本结算，维护候选与模拟研究证据。 |
| 有事前登记和真实开平仓、费用证据 | 用下述脚本记录真实闭环，按真实账本重建派生摘要。 |

## 真实成交如何登记

[trade_learning.py](../scripts/trade_learning.py) 接收一次买入开仓、一次全部退出的买方期权闭环。只记录最终决策事实、证据和有限归因标签。

1. **入场前**，按[意图契约](../schemas/trade_intent.v1.schema.json)登记 `trade_id`、对应的 `decision_id`、标的月份、期权合约、`long_call|long_put`、入场路径、市场状态和支持决策的证据。登记时间来自脚本运行时北京时间。
2. **全部平仓后**，按[结果契约](../schemas/trade_outcome.v1.schema.json)提供与意图一致的 ID、合约和路径，真实的入场、退出、手续费证据，以及适用的其他现金成本。事后补登记、未平仓或无法核实成本的交易，不能补成前向闭环。

按实际证据根目录执行，例如：

```bash
.venv/bin/python scripts/trade_learning.py register-intent --input PATH_TO_INTENT.json --evidence-root PATH_TO_EVIDENCE_ROOT
.venv/bin/python scripts/trade_learning.py append-outcome --input PATH_TO_OUTCOME.json --evidence-root PATH_TO_EVIDENCE_ROOT
.venv/bin/python scripts/trade_learning.py verify
.venv/bin/python scripts/trade_learning.py rebuild
```

输入示例见[交易意图模板](../templates/trade-intent.v1.template.json)和[平仓结果模板](../templates/trade-outcome.v1.template.json)。写入时核对证据字节的 SHA-256，来源身份另行核实。同一交易同一事件的相同重试幂等，内容冲突拒绝。独立保留的账本头哈希可用于检查尾部截断，单靠现存链条只能核验现存事件的一致性。

净收益以真实权利金差 × 手数 × 合约乘数，减去手续费和其他现金成本。滑点已包含在真实成交价里，单列为执行质量观察。按净收益正、负、零记录盈利、亏损或持平，使用契约对应的归因标签。金额按十进制表示。

`rebuild` 从追加账本重建 Markdown 与[摘要](../schemas/trade_learning_summary.v1.schema.json)。单个标签至少在 5 笔同类闭环、2 个品种出现后才列为 `candidate_lesson`。升级为通用规则须复核全部证据、相反样本、成本和可迁移性，提出可证伪、可回滚的具体规则，再取得用户对这项变更的明确授权，开展行为测试与前向观察。
