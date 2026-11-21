# Qice Derivatives Lab · 期策

**本地运行的商品期货与买方期权研究工作台。** 期策把行情取证、Codex 分析、策略校验和结果回看接成一条研究流程。每次分析固定品种与时点，记录可用证据及缺口，再由程序校验结论是否满足发布条件。当前配置覆盖中国商品市场的 22 个品种。

`React · TypeScript`　`FastAPI · Python`　`SQLite`　`Codex CLI`

## 快速体验

需要 macOS 或 Linux、Python 3.12+、Node.js 22+ 和 pnpm。首次启动会联网安装依赖和构建界面。

```bash
git clone https://github.com/aniwar1/qice-derivatives-lab.git
cd qice-derivatives-lab
./start.sh
```

打开 <http://127.0.0.1:8000>，点击「研究品种」中的任一品种，可查看带来源和北京时间的公开连续行情。研究策略前，安装 Codex CLI 并执行 `codex login`，然后选择「全市场」或单个品种，点击「更新最新策略」。

服务只监听回环地址，按 `Ctrl+C` 停止。端口冲突时可用 `./start.sh --port 8080`。可用 `PYTHON_BIN=python3.12 ./start.sh` 指定解释器。行情查询和生成新策略需要联网，已保存的策略可离线回看。

## 从行情到策略

```text
React 工作台 ──提交研究──▶ FastAPI ──创建任务──▶ SQLite
                             │
                             ├──▶ 临时工作区 · 行情取证 · Codex 分析
                             │                    │
                             │           结构化策略与反证结论
                             │                    ▼
                             └──◀── Schema、业务规则与时点校验
                             │
React 工作台 ◀──SSE 进度 / 状态轮询──┴──▶ 已校验策略与公开任务事件
```

独立的报价接口显示品种连续行情。正式研究按实际交割月绑定证据，全市场逐品种记录覆盖与缺口，工作台保存一条通过校验的焦点结论。证据不足时可保存「等待」及原因，供下一轮复核。

## 工程设计

| 问题 | 实现 | 源码入口 |
| --- | --- | --- |
| 怎样约束模型输出？ | Codex 在每轮临时工作区取证并返回结构化策略。服务复验品种、月份、证据时点、反证结论和执行权限，通过后才更新最新策略。 | [任务编排与校验](backend/app/service.py)、[输出契约](backend/schemas/codex_strategy_decision.agent.schema.json) |
| 半根日线能支持什么结论？ | 对夜盘后的部分日线，验证交易所日历、数据覆盖和采集时点。满足条件时可用于条件触发，确认突破要求完成日线。 | [日线证据校验](scripts/session_aware_bar.py)、[边界回归](tests/test_session_aware_bar.py) |
| 两次点击或服务重启会怎样？ | SQLite 部分唯一索引限制同一时间只有一个活动任务。结果与完成事件同事务提交，重启后将遗留任务标记为中断，并继续提供最近已完成的策略。 | [任务存储](backend/app/db.py)、[状态回归](backend/tests/test_store.py) |
| 浏览器断线如何恢复？ | SSE 推送公开进度，前端通过任务状态轮询补偿断线，任务结果由服务端保存。 | [SSE](backend/app/sse.py)、[前端状态处理](frontend/src/App.tsx) |

运行数据默认位于 `local-data/`，SQLite 文件为 `local-data/qice.sqlite3`。工作台用于研究，交易账户与订单执行在系统范围之外。独立准备的历史日线缓存须通过完整性校验，缺失时按证据缺口处理。

## 验证与源码导航

本机验收覆盖从干净检出启动、浏览器查询公开报价、真实 Codex 单品种期权研究，以及重启后读回策略。回归测试覆盖过期证据、非法合约、重复任务、失败时保留旧策略及期权校验边界。全市场扫描已有测试数据回归，真实行情的端到端验收覆盖单品种。

```bash
.venv/bin/python -m pip install -r backend/requirements-dev.txt
PYTHONPATH=backend:. .venv/bin/python -m pytest -q backend/tests tests
pnpm --dir frontend test
pnpm --dir frontend build
```

默认检出跳过依赖已发布历史日线缓存的测试。启动和策略研究的环境条件见上文。

- [研究工作流](SKILL.md)与[证据约定](references/evidence-contract.md)
- [市场采集与研究脚本](scripts/)及[研究配置](config/)
