#!/usr/bin/env bash

set -euo pipefail

REPO_URL="https://github.com/jxxghp/MoviePilot.git"
WORKDIR="$PWD"
APP_DIR_NAME="MoviePilot"
LINK_CLI="true"
LINK_PATH=""
RUN_WIZARD="true"
START_AFTER_INSTALL="true"
NON_INTERACTIVE="false"
OS_NAME="Unknown"
PYTHON_BIN=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --workdir PATH           克隆与安装的目标目录，默认当前目录
  --app-dir NAME           MoviePilot 目录名，默认 ${APP_DIR_NAME}
  --repo-url URL           主项目仓库地址
  --link-path PATH         全局 moviepilot 软链接位置
  --no-link-cli            安装完成后不创建全局 moviepilot 命令
  --no-wizard              跳过 moviepilot setup 的交互式初始化向导
  --no-start               安装完成后不自动启动服务
  --non-interactive        非交互模式，直接使用传入参数
  -h, --help               显示帮助

Examples:
  $(basename "$0")
  $(basename "$0") --workdir ~/Projects
  $(basename "$0") --non-interactive --workdir ~/Projects --no-start
EOF
}

detect_os() {
  local uname_s
  uname_s="$(uname -s)"

  case "$uname_s" in
    Darwin)
      OS_NAME="macOS"
      if command -v brew >/dev/null 2>&1; then
        LINK_PATH="$(brew --prefix)/bin/moviepilot"
      else
        LINK_PATH="/usr/local/bin/moviepilot"
      fi
      ;;
    Linux)
      if grep -qi microsoft /proc/version 2>/dev/null; then
        OS_NAME="Linux (WSL)"
      else
        OS_NAME="Linux"
      fi
      LINK_PATH="/usr/local/bin/moviepilot"
      ;;
    MINGW*|MSYS*|CYGWIN*)
      OS_NAME="Windows"
      ;;
    *)
      OS_NAME="$uname_s"
      LINK_PATH="/usr/local/bin/moviepilot"
      ;;
  esac
}

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

python_version_ok() {
  local python_bin="$1"
  "$python_bin" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
}

python_install_hint() {
  case "$OS_NAME" in
    macOS)
      echo "请先安装 Git、curl 和 Python 3.12，例如：brew install git curl python@3.12" >&2
      ;;
    Linux*)
      echo "请先安装 Git、curl 和 Python 3.12，并确保包含 venv 模块。" >&2
      echo "例如 Debian/Ubuntu: sudo apt install git curl python3.12 python3.12-venv" >&2
      echo "例如 Fedora/RHEL:  sudo dnf install git curl python3.12" >&2
      ;;
    Windows)
      echo "推荐在 WSL、Linux 或 macOS 终端中运行此脚本。" >&2
      ;;
    *)
      echo "请先安装 Git、curl 和 Python 3.12。" >&2
      ;;
  esac
}

require_prereqs() {
  if [[ "$OS_NAME" == "Windows" ]]; then
    echo "检测到当前环境为 Windows shell，建议改用 WSL、Linux 或 macOS 终端运行。" >&2
    exit 1
  fi

  if ! command -v git >/dev/null 2>&1; then
    echo "未找到 git。" >&2
    python_install_hint
    exit 1
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "未找到 curl。" >&2
    python_install_hint
    exit 1
  fi

  PYTHON_BIN="$(find_python || true)"
  if [[ -z "$PYTHON_BIN" ]] || ! python_version_ok "$PYTHON_BIN"; then
    echo "未找到可用的 Python 3.12+ 解释器。" >&2
    python_install_hint
    exit 1
  fi
}

prompt_text() {
  local label="$1"
  local default_value="${2:-}"
  local answer=""

  if [[ -n "$default_value" ]]; then
    read -r -p "$label [$default_value]: " answer || true
    if [[ -z "$answer" ]]; then
      answer="$default_value"
    fi
  else
    read -r -p "$label: " answer || true
  fi

  printf '%s\n' "$answer"
}

prompt_yes_no() {
  local label="$1"
  local default_value="${2:-y}"
  local answer=""
  local prompt="[y/N]"

  if [[ "$default_value" == "y" ]]; then
    prompt="[Y/n]"
  fi

  while true; do
    read -r -p "$label $prompt: " answer || true
    answer="${answer,,}"
    if [[ -z "$answer" ]]; then
      answer="$default_value"
    fi
    case "$answer" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
    esac
    echo "请输入 y 或 n。"
  done
}

run_interactive_guide() {
  echo "==> 当前系统: $OS_NAME"
  echo "==> 将自动拉取 MoviePilot，并下载前端 release、资源文件与本地 Node 运行时"

  WORKDIR="$(prompt_text "安装目录" "$WORKDIR")"
  APP_DIR_NAME="$(prompt_text "主项目目录名" "$APP_DIR_NAME")"

  if prompt_yes_no "安装过程中进入 MoviePilot 初始化向导" "y"; then
    RUN_WIZARD="true"
  else
    RUN_WIZARD="false"
  fi

  if prompt_yes_no "安装完成后立即启动前后端服务" "y"; then
    START_AFTER_INSTALL="true"
  else
    START_AFTER_INSTALL="false"
  fi
}

ensure_link_path() {
  if [[ "$LINK_CLI" != "true" ]]; then
    return
  fi

  if [[ -z "$LINK_PATH" ]]; then
    LINK_PATH="/usr/local/bin/moviepilot"
  fi

  local link_dir
  link_dir="$(dirname "$LINK_PATH")"
  if mkdir -p "$link_dir" 2>/dev/null && [[ -w "$link_dir" ]]; then
    return
  fi

  LINK_PATH="$HOME/.local/bin/moviepilot"
  mkdir -p "$(dirname "$LINK_PATH")"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir)
      WORKDIR="$2"
      shift 2
      ;;
    --app-dir)
      APP_DIR_NAME="$2"
      shift 2
      ;;
    --repo-url)
      REPO_URL="$2"
      shift 2
      ;;
    --link-path)
      LINK_PATH="$2"
      shift 2
      ;;
    --no-link-cli)
      LINK_CLI="false"
      shift
      ;;
    --no-wizard)
      RUN_WIZARD="false"
      shift
      ;;
    --no-start)
      START_AFTER_INSTALL="false"
      shift
      ;;
    --non-interactive)
      NON_INTERACTIVE="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage
      exit 1
      ;;
  esac
done

detect_os
require_prereqs
ensure_link_path

if [[ "$NON_INTERACTIVE" != "true" && -t 0 && -t 1 ]]; then
  run_interactive_guide
  ensure_link_path
fi

mkdir -p "$WORKDIR"
WORKDIR="$(cd "$WORKDIR" && pwd)"
APP_DIR="$WORKDIR/$APP_DIR_NAME"

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "==> 克隆 MoviePilot 到 $APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
else
  echo "==> 复用已有 MoviePilot 仓库: $APP_DIR"
fi

cd "$APP_DIR"
echo "==> 执行本地环境安装与初始化"
SETUP_ARGS=(setup)
if [[ "$RUN_WIZARD" == "true" ]]; then
  SETUP_ARGS+=(--wizard)
fi
./moviepilot "${SETUP_ARGS[@]}"

if [[ "$LINK_CLI" == "true" ]]; then
  echo "==> 创建全局 moviepilot 命令到 $LINK_PATH"
  ln -sf "$APP_DIR/moviepilot" "$LINK_PATH"
fi

if [[ "$START_AFTER_INSTALL" == "true" ]]; then
  echo "==> 启动 MoviePilot 前后端服务"
  ./moviepilot start
fi

cat <<EOF
==> 安装完成

系统环境: $OS_NAME
项目目录: $APP_DIR
Python 解释器: $PYTHON_BIN
CLI 命令: ${LINK_CLI:-false}
CLI 路径: ${LINK_PATH:-未创建}

使用方式:
  moviepilot status
  moviepilot logs --frontend
  moviepilot logs --stdio

完整 CLI 文档:
  $APP_DIR/docs/cli.md
EOF
