#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  qice_python="${PYTHON_BIN:-python3}"
  if ! "$qice_python" -c 'import sys; assert sys.version_info >= (3, 12)' 2>/dev/null; then
    echo '需要 Python 3.12 或更新版本，可用 PYTHON_BIN 指定解释器。' >&2
    exit 1
  fi
  "$qice_python" -m venv .venv
fi
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH="$PWD/backend:$PWD${PYTHONPATH:+:$PYTHONPATH}"

# 有完整构建时直接复用，首次安装或依赖变更时重新准备。
qice_python_hash=$(cat requirements.txt backend/requirements.txt | shasum -a 256 | cut -d ' ' -f 1)
if [ "$(cat .venv/.qice-dependencies 2>/dev/null || true)" != "$qice_python_hash" ]; then
  python -m pip install -r requirements.txt -r backend/requirements.txt
  printf '%s\n' "$qice_python_hash" > .venv/.qice-dependencies
fi

qice_frontend_hash=$(find frontend/src -type f -print0 | sort -z | xargs -0 shasum -a 256 | cat - <(shasum -a 256 frontend/*.json frontend/*.yaml frontend/*.ts frontend/index.html) | shasum -a 256 | cut -d ' ' -f 1)
if [ ! -f frontend/dist/index.html ] || [ "$(cat frontend/dist/.qice-build 2>/dev/null || true)" != "$qice_frontend_hash" ]; then
  if ! node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)' 2>/dev/null || ! command -v pnpm >/dev/null; then
    echo '首次构建界面需要 Node.js 22 或更新版本，以及 pnpm。' >&2
    exit 1
  fi
  pnpm --dir frontend install --frozen-lockfile
  pnpm --dir frontend build
  printf '%s\n' "$qice_frontend_hash" > frontend/dist/.qice-build
fi

exec python -m app.serve "$@"
