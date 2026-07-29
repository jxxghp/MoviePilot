#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MOVIEPILOT_BIN="$PROJECT_ROOT/moviepilot"
VENV_PYTHON="$PROJECT_ROOT/venv/bin/python"

show_usage() {
  cat <<'EOF'
用法：
  ./scripts/start-local.sh                         启动后端开发服务（前台运行）
  ./scripts/start-local.sh backend                 启动后端开发服务（前台运行）
  ./scripts/start-local.sh service start          启动后端和已安装的前端服务
  ./scripts/start-local.sh service start --safe   以安全模式启动完整服务
  ./scripts/start-local.sh stop|restart|status    管理后端和前端服务
  ./scripts/start-local.sh logs [OPTIONS]          查看后端日志
  ./scripts/start-local.sh help                   显示本帮助
EOF
}

if [[ ! -x "$MOVIEPILOT_BIN" ]]; then
  printf '未找到本地 CLI：%s\n' "$MOVIEPILOT_BIN" >&2
  exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  printf '未找到项目虚拟环境：%s\n请先执行：%s install deps\n' "$VENV_PYTHON" "$MOVIEPILOT_BIN" >&2
  exit 1
fi

# 显式传入配置目录，避免被仓库中的临时 .moviepilot.env 覆盖。
if [[ -z "${CONFIG_DIR:-}" ]]; then
  if [[ -n "${MOVIEPILOT_CONFIG_DIR:-}" ]]; then
    CONFIG_DIR="$MOVIEPILOT_CONFIG_DIR"
  elif [[ -d "${HOME:-}/Documents/moviepilot" ]]; then
    CONFIG_DIR="${HOME}/Documents/moviepilot"
  else
    CONFIG_DIR="$PROJECT_ROOT/config"
  fi
fi
export CONFIG_DIR
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export DEBUG="${DEBUG:-true}"
export DEV="${DEV:-true}"

cd "$PROJECT_ROOT"

if [[ "$#" -eq 0 ]]; then
  set -- backend
fi

command_name="$1"
shift

case "$command_name" in
  backend|start)
    if [[ "$#" -gt 0 ]]; then
      printf '后端模块启动不接受额外参数；完整服务请使用：%s service start [OPTIONS]\n' "$0" >&2
      exit 2
    fi
    exec "$VENV_PYTHON" -m app.main
    ;;
  service)
    if [[ "$#" -eq 0 ]]; then
      set -- start
    fi
    exec "$MOVIEPILOT_BIN" "$@"
    ;;
  stop|restart|status|logs|doctor|config|version)
    exec "$MOVIEPILOT_BIN" "$command_name" "$@"
    ;;
  help|--help|-h)
    show_usage
    ;;
  *)
    printf '未知命令：%s\n\n' "$command_name" >&2
    show_usage >&2
    exit 2
    ;;
esac
