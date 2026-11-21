# 期货标的报告 futures_main_report.v3

仅在生成或校验结构化期货来源报告时读取。每份 v3 聚焦一个实际合约。`action=long|short|wait` 表达该报告的判断；具体买方期权及其资本动作另用[期权契约](long-option-entry.md#正式输出)。普通行情复盘按[历史复盘方法](historical-replay.md)交付，不需生成当下可交易的 v3。

字段白名单、证据复算和拒绝条件以[报告校验器](../scripts/futures_main_report.py)为准。单品种、全市场样例见[合同测试](../tests/test_decision_contract.py)中的 `base_report` 和 `make_all_market_report`。用报告目录作证据根运行：

```bash
.venv/bin/python scripts/futures_main_report.py path/to/report.json
```

## 范围与扫描

报告顶层绑定版本、北京时间观察时点、扫描范围、动作、tick、target、实际主力、行情和结构证据、基本面、新闻、会员、点位计划及综合判断。不额外添加分数、投票、机器授权等字段。单品种使用 `single_product`；全市场使用 `all_market`，并满足以下绑定。

- `scan_manifest` 为 [scan_market.py](../scripts/scan_market.py) 生成的完整 `scan_manifest.v2`，`scan_manifest_sha256` 为其紧凑 canonical JSON 哈希。配置 universe、政策与规格哈希一致，每个配置品种恰有一行，成功、部分及不可观测三类计数由实际行复算。保留各行原始 raw/structure 内容寻址文件，检索失败不能重写成成功。
- 区分采集批次时长与决策可用时长，分别受 `acquisition_max_batch_span_seconds` 和 `decision_use_max_age_seconds` 约束。报告时重新取得焦点行情。`focus_refresh_from_scan` 绑定原清单哈希、品种、行号、原合约、流动性与文件哈希；刷新请求不早于原行取证完成时间。确有移仓/重选则记录前后合约及原因。
- `candidate_comparison` 将焦点与至少一个被否决或观察的候选绑定到各自扫描行、缺口、证据和机会成本。流动性与机器排序不决定方向。默认更新可有多份这样的报告，每个合格机会使用独立契约，同轮机会可共用完整扫描证据；单焦点宿主只按其实际保存能力说明交付范围。

## 合约身份与行情

- `actual_main` 模式从规范化品种入口（如 `JD0`）按同批实际月份的持仓量、成交量与合约代码排序，`analysis_contract` 等于识别出的主力。显式指定月份使用 `user_specified_contract`，直接请求该实际合约并给出 `roll_context`。不拿主力点位替换指定月。
- 重放原始请求与来源身份。满足 `request_started_at <= chain_observed_at <= fetch_time <= report.observed_at`，每一行与配置交易所匹配。全合约链、分页与来源总数均闭合才可称 `complete`；部分链的首位只是已见合约中的首位。并列、未知 OI 或时点不可比时披露临时识别。成交量为零、缺失分别为 `no_trade`、`unobservable`，不能拿更易交易的月份冒充主力。
- 当前焦点行情与主力链须为同批，保留快照路径与哈希。过时或无法绑定时标 `historical_or_unbound`。仅显式指定月可独立于不完整的主力证明核验自身行情。
- `market_data.status` 区分 fresh、stale、unobservable。`order_timing=now` 需在政策时窗内重放 actual contract 行的价格、时间、正成交量与非倒挂、tick 对齐的 bid/ask；按方向评估可成交侧、价差、入场区间。合约零成交或只有结算数据，v3 动作为 `wait`。正成交量但当前盘口缺失，只能保留有结构化 `execution_precondition` 的条件计划，要求执行前重抓行情、正成交量与可成交双边价。
- 宽筛使用 `configured_research`，绑定规格文件哈希与条目定位符。单品种 `third_party_cross_check` 仅在具体合约详情页归档的代码、品种、交易所、表格 tick 与当前规格全部匹配时提供 `exact_actual_contract`。该值表示本次归档和配置一致；委托精度另需交易所或柜台核实。不一致则保留警告并降级到 `product_specs_snapshot_proxy`。近似 tick 可用于研究，不能冒充正式方向动作所需精度。CZCE 出站代码使用规范四位年月；旧三位入站码需要参考日期才能转换。

## 日线结构与历史确认

- 有结构时绑定实际合约、原始快照路径和 SHA、artifact 哈希、算法、tick、as-of 及 `structure_id`。校验器重跑算法核对方向、支撑、压力和趋势点。不可观测时写来源尝试、`direction=unobservable`、`levels=null` 和 `point_plan=null`，保留研究观点及缺口。
- `completed_daily_bar_bound_to_raw_bytes` 仅证明最新完成 K 线等于已验 raw。要声明 `daily_cache_artifact_replay_verified`，须在文件输入下重放 manifest、独立锚、gzip 对象及合约切片。上游响应身份另验 `upstream_response_identity_verified`。单独传 Python mapping 只验内部一致性。日线降级须标 `daily_data_evidence_not_verified`，用 `degraded_raw_completed_daily_row` 和相应 provenance，不贴官方来源标签。
- DCE 新浪日线正文没有合约身份。归档须含完整有界响应、请求及最终 URL、状态和哈希，另用带身份的同日实际合约行情在规则允许的日盘收盘后窗口核对 OHLC、成交量和持仓。旧 manifest 缺来源响应归档时，字节完整性与身份可信度分别记录，不能因来源身份缺失抹掉其他已验证事实。字段及证明细节见[日线加载器](../scripts/verified_daily_loader.py)。
- 当前交易日的日线叠加层与历史枢轴分开。`daily_bar_basis` 须绑定技术证据包、状态、时点与允许语义。partial 的交易所日历、完整覆盖、用途及降级见[共用边界](invariants.md)和[证据合同](evidence-contract.md)。
- 要声称点位已在过去的完成日线上得到确认，保存独立的 S1、S2。S1 冻结触发边界，最后完成日早于 S2，有独立的日线证据锚；S2 保存确认日至当前最近完成日的连续同合约 K 线及当前行情。验证重叠区的日期、OHLC、量、仓一致，确认 K 线不能同时触发失效。只写旧确认日期不能替代双快照。`pending` 方向动作需 `timing_override_rationale`，已失效则重建计划。

## 基本面、消息与会员

每条来源保留 URL、检索时点、内容 SHA256、行号或段落定位。基本面条目说明数据日期、影响月份、传导和反证；新闻区分 `material_events_found`、`search_completed_no_material_news` 和 `unobservable`。完成检索却无重大事件时记录查询词、来源、检索时间和判断标准；检索未完成时保留不可观测。

会员窗口、Top20 增减与具名席位基线见[会员口径](member-flow-principles.md)。正式 v3 要求可观测窗口的 `prior_session_context` 绑定前一完成日的同合约收盘、总持仓及交易所来源。具名席位 `prior_positions` 只覆盖可观测侧，Top20 使用披露的组成与增减口径。第三方回退即使能支持研究，缺少契约要求的来源时仍不能宣称该正式字段已验。

## 判断、计划与校验结果

- `agent_judgment` 按 `technical_structure`、`fundamentals_and_news`、`member_flow` 记录两侧理由、证据引用与因果传导。面向用户时仍分别说明基本面、资金面、消息面及技术结构。置信度数值表示附理由的判断，不是校准过的概率。
- 比较 long、short、wait，解释 `why_now`、`why_not_wait`、`why_not_opposite`、最强反证、可证伪条件与成本后的结果分布。正式方向动作需点位计划及有依据的正期望判断；校验入场、失效、目标和赔率，但没有固定 2R 门槛。若动作与观点不同，按契约交代原因。等待时填 `wait_for`、未来的 `reassess_by` 和 `update_triggers`。
- 校验成功只记录 `agent_direction_recorded` 或 `agent_wait_recorded`。始终保持 `validation_is_trade_permission=false`、`machine_selected_action=false` 和 `futures_order_authorized=false`。
