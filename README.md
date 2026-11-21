# 期策 · 本地研究工作台

期策是一个在本机运行的商品期货和买方期权研究工具。界面提供 22 个研究品种、公开参考报价、最新策略和运行进度。SQLite 在本地保存已校验的策略和任务状态。全市场筛选和单品种分析按需调用 Codex。

## 本地启动

需要 macOS 或 Linux、Python 3.12+、Node.js 22+ 和 pnpm。首次启动联网安装依赖和构建界面，之后可以直接复用已安装的依赖和已构建的界面。

```bash
git clone https://github.com/aniwar1/qice-local.git
cd qice-local
./start.sh
```

打开 <http://127.0.0.1:8000>。按 `Ctrl+C` 停止。端口冲突可用 `./start.sh --port 8080`，使用其他 Python 解释器时可设 `PYTHON_BIN=python3.12`。

## 使用

1. 在「研究品种」点击品种，查询带来源和北京时间的参考报价。网络或行情服务故障会显示读取失败。
2. 需要生成策略时，先安装 Codex CLI 并执行 `codex login`，然后选择「全市场」或一个品种，点击「更新最新策略」。系统在独立的临时目录取证，并在结果通过校验后保存。已有策略可在离线时回看。
3. 本地数据库位于 `local-data/qice.sqlite3`。停止服务后可备份 `local-data/`。历史日线缓存由使用者另行准备，只有符合完整性合同的缓存才作为已验证数据使用。行情缺口会写入策略依据。

报价、策略与仓位复核都用于研究。程序不连接券商交易账户，也不发送订单。

## 源码与验证

- `frontend/` 是 React + TypeScript 界面。
- `backend/` 是 FastAPI 服务及 SQLite 持久化。
- `scripts/`、`config/`、`schemas/` 是 Python 研究流程及配置合同。

```bash
.venv/bin/python -m pip install -r backend/requirements-dev.txt
PYTHONPATH=backend:. .venv/bin/python -m pytest -q backend/tests tests
pnpm --dir frontend test
pnpm --dir frontend build
```

依赖私有历史行情缓存的集成用例在全新克隆时跳过。其他测试的行情和策略样本在运行时生成或使用固定测试输入。
