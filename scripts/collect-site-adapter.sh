#!/usr/bin/env bash

set -euo pipefail

ORIGINAL_DIR="$PWD"
SCRIPT_DIR=""
PROJECT_ROOT=""
PYTHON_BIN=""
COLLECTOR_PATH=""

# 判断候选 Python 是否满足 3.11 最低版本。
python_version_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

# 从当前源码目录、环境变量或脚本位置查找 MoviePilot 根目录。
find_project_root() {
  local candidate=""
  local source_path="${BASH_SOURCE[0]:-}"
  if [[ -n "$source_path" && -f "$source_path" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "$source_path")" && pwd)"
  fi
  for candidate in "${MOVIEPILOT_ROOT:-}" "$ORIGINAL_DIR" "${SCRIPT_DIR:+$SCRIPT_DIR/..}"; do
    if [[ -n "$candidate" && -f "$candidate/scripts/site_adapter_collector.py" ]]; then
      PROJECT_ROOT="$(cd "$candidate" && pwd)"
      return 0
    fi
  done
  return 1
}

# 查找可用的项目虚拟环境或系统 Python。
find_python() {
  local candidate=""
  local resolved=""
  for candidate in \
    "${PROJECT_ROOT:+$PROJECT_ROOT/venv/bin/python}" \
    "${PROJECT_ROOT:+$PROJECT_ROOT/.venv/bin/python}" \
    "${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}" \
    python3.13 python3.12 python3.11 python3; do
    [[ -n "$candidate" ]] || continue
    if [[ "$candidate" == */* ]]; then
      resolved="$candidate"
    else
      resolved="$(command -v "$candidate" 2>/dev/null || true)"
    fi
    if [[ -n "$resolved" && -x "$resolved" ]] && python_version_ok "$resolved"; then
      PYTHON_BIN="$resolved"
      return 0
    fi
  done
  return 1
}

# 判断 Python 是否已具备独立采集器所需的最小运行依赖。
project_runtime_ready() {
  PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import requests
import websocket
from bs4 import BeautifulSoup
PY
}

# 让脚本始终从终端安全读取交互输入。
restore_terminal_input() {
  if [[ -r /dev/tty ]]; then
    exec </dev/tty
  else
    echo "需要可交互终端读取站点地址并等待浏览器采集确认。" >&2
    exit 1
  fi
}

# 定位本地项目运行环境后启动随发行包提供的采集器。
main() {
  if ! find_project_root; then
    echo "未找到本地 MoviePilot 源码或安装目录，请在 MoviePilot 目录中运行此脚本。" >&2
    exit 1
  fi
  if ! find_python; then
    echo "未找到 Python 3.11 或更高版本，请先安装 Python。" >&2
    exit 1
  fi

  if ! project_runtime_ready; then
    echo "本地 Python 环境缺少采集器依赖，请优先下载官方 Release 的独立采集器。" >&2
    exit 1
  fi
  COLLECTOR_PATH="$PROJECT_ROOT/scripts/site_adapter_collector.py"

  restore_terminal_input
  cd "$ORIGINAL_DIR"
  PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" "$COLLECTOR_PATH" "$@"
}

main "$@"
