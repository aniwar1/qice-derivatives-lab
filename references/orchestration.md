# 正式决策的取证与校验

[请求入口](../SKILL.md)决定工作路径。本页用于生成正式决策工件。历史行情回放、模拟协议与 Skill 维护独立执行；不要为了满足表格格式把它们伪装成实盘交易或实时决策。

## 建立 run

支持的 `task_mode`、所需证据与禁止行为以[编排政策](../config/decision_orchestration_policy.json)和[运行契约](../schemas/decision_run.v1.schema.json)为准。用 `scripts/run_decision.py init --mode … --run-id … --output …` 冻结输入；单合约判断还传 `--product`、`--requested-contract`，按需加 `--option-analysis`。为某月期权分析现有仓位时绑定其真实标的，不用换月后的主力替代。

1. **选品并绑定合约。** 全市场先运行 `scripts/refresh_daily_market_data.py`、`scripts/scan_market.py`，用 `configured_research` 规格宽筛，再每批核验 1–3 个候选，合格总数不限。单品种用 `scripts/fetch_futures.py`、`scripts/market_structure.py`；第三方规格与配置冲突时保留降级标识，不用它证明报单精度。
2. **独立取证。** 确定分析月份后，技术结构、基本面与消息、会员资金、按需期权链可并行；分支返回证据包，不修改同一个 run，不先限定寻找 call 或 put 的证据。[证据合同](evidence-contract.md)定义五轴状态和来源回退。
3. **解释并核验。** Agent 比较多空与等待，解释为何现在、为何不取另一方向、最强反证与失效条件，形成 `underlying_thesis.v1`。如使用当前交易日 bar，精确绑定 `daily_bar_basis`；盘中报价只能叠加，`session_partial` 仅限常规夜盘收盘后满足日历/覆盖条件的品种，不能替代日线确认。
4. **需要期权时再形成表达。** 用 `scripts/validate_option_decision.py` 检查每个单独的 `option_decision.v1`；标的报告不承载期权执行字段。正式 run 用 `attach-evidence`、`attach-thesis`、按需 `attach-option` 附入工件。未知成本/身份等使正式契约无法成立时，保留已核事实与具体缺口，不编数凑齐字段。
5. **复核并结束。** 确定性校验之后，独立反证核查因果、反例及推送资格，不投票。最多一次针对缺陷的修订；仍有实质性冲突就不作相应执行声明。用 `critic` 记录审查，仅合格的 run 才能 `finalize`。一个机会一个契约，不用单焦点 schema 限制整份机会清单。

## 发布与监测边界

用户要求生成离线最新策略时，`scripts/update_latest_strategy.py` 只接受已完成、反证已解决、日线依据显式且有效的 run；失败不覆盖旧产物。[本地工作台](../README.md)使用临时取证目录，在 SQLite 中保存通过校验的单焦点结论。

生命周期状态和合法迁移见编排政策。静态报告不能自行观察实时止损，只有再次查询或已授权的监测才会复核；两者都不下单。
