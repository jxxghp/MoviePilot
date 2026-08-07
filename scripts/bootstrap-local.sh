#!/usr/bin/env bash

set -euo pipefail

REPO_URL="https://github.com/jxxghp/MoviePilot.git"
REPO_REF="v3"
WORKDIR="$PWD"
APP_DIR_NAME="MoviePilot"
LINK_CLI="true"
LINK_PATH=""
CONFIG_DIR=""
RUN_WIZARD="true"
START_AFTER_INSTALL="true"
NON_INTERACTIVE="false"
SUPERUSER=""
SUPERUSER_PASSWORD=""
OS_NAME="Unknown"
PYTHON_BIN=""
BREW_BIN=""
PACKAGE_MANAGER=""
PACKAGE_INDEX_UPDATED="false"
PROMPT_INPUT="/dev/stdin"
PROMPT_OUTPUT="/dev/stdout"
HAS_TTY="false"
PATH_RC_FILE=""
PATH_UPDATED="false"

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --workdir PATH           克隆与安装的目标目录，默认当前目录
  --app-dir NAME           MoviePilot 目录名，默认 ${APP_DIR_NAME}
  --repo-url URL           主项目仓库地址
  --config-dir PATH        配置目录，默认使用程序目录外的系统配置目录
  --superuser NAME         预设超级管理员用户名
  --superuser-password PWD 预设超级管理员密码
  --link-path PATH         全局 moviepilot 软链接位置
  --no-link-cli            安装完成后不创建全局 moviepilot 命令
  --no-wizard              跳过 moviepilot setup 的交互式初始化向导
  --no-start               安装完成后不自动启动服务
  --non-interactive        非交互模式，直接使用传入参数
  -h, --help               显示帮助

Examples:
  $(basename "$0")
  $(basename "$0") --workdir ~/Projects
  $(basename "$0") --config-dir ~/.config/moviepilot-local
  $(basename "$0") --superuser admin --superuser-password 'ChangeMe123!'
  $(basename "$0") --non-interactive --workdir ~/Projects --no-start
EOF
}

repo_dirty() {
  (
    cd "$1"
    git status --porcelain --untracked-files=no 2>/dev/null | grep -q .
  )
}

sync_repo() {
  if [[ ! -d "$APP_DIR/.git" ]]; then
    echo "==> 克隆 MoviePilot 到 $APP_DIR"
    git clone --branch "$REPO_REF" "$REPO_URL" "$APP_DIR"
    return
  fi

  echo "==> 复用已有 MoviePilot 仓库: $APP_DIR"
  if repo_dirty "$APP_DIR"; then
    echo "检测到现有仓库包含未提交改动，已停止自动更新。" >&2
    echo "请先清理 $APP_DIR 的本地修改，或换一个新的安装目录后重试。" >&2
    exit 1
  fi

  (
    cd "$APP_DIR"
    echo "==> 更新本地仓库到 origin/$REPO_REF"
    git fetch --tags origin "$REPO_REF"
    if git show-ref --verify --quiet "refs/heads/$REPO_REF"; then
      git checkout "$REPO_REF"
    else
      git checkout -b "$REPO_REF" "origin/$REPO_REF"
    fi
    git pull --ff-only origin "$REPO_REF"
  )
}

default_config_dir() {
  case "$OS_NAME" in
    macOS)
      printf '%s\n' "$HOME/Library/Application Support/MoviePilot"
      ;;
    *)
      printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/moviepilot"
      ;;
  esac
}

setup_prompt_io() {
  if [[ -t 0 && -t 1 ]]; then
    HAS_TTY="true"
    return
  fi

  if [[ -r /dev/tty && -w /dev/tty ]]; then
    PROMPT_INPUT="/dev/tty"
    PROMPT_OUTPUT="/dev/tty"
    HAS_TTY="true"
  fi
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

  if [[ -z "$CONFIG_DIR" ]]; then
    CONFIG_DIR="$(default_config_dir)"
  fi
}

detect_package_manager() {
  case "$OS_NAME" in
    macOS)
      PACKAGE_MANAGER="brew"
      ;;
    Linux*)
      if command -v apt-get >/dev/null 2>&1; then
        PACKAGE_MANAGER="apt-get"
      elif command -v dnf >/dev/null 2>&1; then
        PACKAGE_MANAGER="dnf"
      elif command -v yum >/dev/null 2>&1; then
        PACKAGE_MANAGER="yum"
      elif command -v zypper >/dev/null 2>&1; then
        PACKAGE_MANAGER="zypper"
      elif command -v pacman >/dev/null 2>&1; then
        PACKAGE_MANAGER="pacman"
      elif command -v apk >/dev/null 2>&1; then
        PACKAGE_MANAGER="apk"
      else
        PACKAGE_MANAGER=""
      fi
      ;;
    *)
      PACKAGE_MANAGER=""
      ;;
  esac
}

python_version_ok() {
  local python_bin="$1"
  "$python_bin" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

try_python_candidate() {
  local candidate="$1"
  local python_path=""

  python_path="$(command -v "$candidate" 2>/dev/null || true)"
  if [[ -n "$python_path" ]] && python_version_ok "$python_path"; then
    printf '%s\n' "$python_path"
    return 0
  fi
  return 1
}

find_python() {
  local minor=""
  for minor in 20 19 18 17 16 15 14 13 12 11; do
    if try_python_candidate "python3.$minor"; then
      return 0
    fi
  done
  if try_python_candidate python3; then
    return 0
  fi
  if try_python_candidate python; then
    return 0
  fi
  return 1
}

find_uv_python() {
  local uv_bin="$1"
  local minor=""
  local python_path=""

  for minor in 20 19 18 17 16 15 14 13 12 11; do
    python_path="$("$uv_bin" python find "3.$minor" 2>/dev/null || true)"
    if [[ -n "$python_path" ]] && python_version_ok "$python_path"; then
      printf '%s\n' "$python_path"
      return 0
    fi
  done
  return 1
}

python_install_hint() {
  case "$OS_NAME" in
    macOS)
      echo "脚本已尝试自动安装 Git、curl 和 Python 3.11+。" >&2
      echo "如果自动安装失败，请先安装 Homebrew，或手动执行：brew install git curl python@3.11" >&2
      ;;
    Linux*)
      echo "脚本已尝试自动安装 Git、curl 和 Python 3.11+。" >&2
      echo "如果自动安装失败，请先安装 Git、curl、Python 3.11+，并确保包含 venv 模块。" >&2
      echo "例如 Debian/Ubuntu: sudo apt install git curl python3.11 python3.11-venv" >&2
      echo "例如 Fedora/RHEL:  sudo dnf install git curl python3.11" >&2
      ;;
    Windows)
      echo "推荐在 WSL、Linux 或 macOS 终端中运行此脚本。" >&2
      ;;
    *)
      echo "请先安装 Git、curl、Python 3.11 或更高版本。" >&2
      ;;
  esac
}

setup_brew_env() {
  local candidate=""
  for candidate in "$BREW_BIN" "$(command -v brew 2>/dev/null || true)" /opt/homebrew/bin/brew /usr/local/bin/brew /home/linuxbrew/.linuxbrew/bin/brew "$HOME/.linuxbrew/bin/brew"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      BREW_BIN="$candidate"
      eval "$("$BREW_BIN" shellenv)"
      return 0
    fi
  done
  return 1
}

ensure_brew() {
  if setup_brew_env; then
    return 0
  fi

  echo "==> 未找到 Homebrew，开始自动安装"
  NONINTERACTIVE=1 CI=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if ! setup_brew_env; then
    echo "自动安装 Homebrew 失败。" >&2
    return 1
  fi
}

run_privileged() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
    return
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "当前步骤需要 sudo 权限，但系统中未找到 sudo。" >&2
    return 1
  fi

  if [[ "$HAS_TTY" == "true" ]]; then
    sudo "$@"
    return
  fi

  if sudo -n true >/dev/null 2>&1; then
    sudo -n "$@"
    return
  fi

  echo "当前步骤需要 sudo 权限，请在可交互终端中重新运行脚本。" >&2
  return 1
}

refresh_package_index() {
  if [[ "$PACKAGE_INDEX_UPDATED" == "true" ]]; then
    return
  fi

  case "$PACKAGE_MANAGER" in
    apt-get)
      run_privileged apt-get update
      ;;
    pacman)
      run_privileged pacman -Sy --noconfirm
      ;;
    zypper)
      run_privileged zypper --gpg-auto-import-keys refresh
      ;;
    apk)
      run_privileged apk update
      ;;
  esac

  PACKAGE_INDEX_UPDATED="true"
}

install_system_packages() {
  local packages=("$@")
  if [[ "${#packages[@]}" -eq 0 ]]; then
    return 0
  fi

  case "$PACKAGE_MANAGER" in
    brew)
      ensure_brew
      "$BREW_BIN" install "${packages[@]}"
      ;;
    apt-get)
      refresh_package_index
      run_privileged apt-get install -y "${packages[@]}"
      ;;
    dnf)
      run_privileged dnf install -y "${packages[@]}"
      ;;
    yum)
      run_privileged yum install -y "${packages[@]}"
      ;;
    zypper)
      refresh_package_index
      run_privileged zypper install -y "${packages[@]}"
      ;;
    pacman)
      refresh_package_index
      run_privileged pacman -S --noconfirm --needed "${packages[@]}"
      ;;
    apk)
      refresh_package_index
      run_privileged apk add --no-cache "${packages[@]}"
      ;;
    *)
      echo "当前系统暂不支持自动安装依赖，请手动安装：${packages[*]}" >&2
      return 1
      ;;
  esac
}

ensure_base_tools() {
  local missing=()

  if ! command -v git >/dev/null 2>&1; then
    missing+=("git")
  fi

  if ! command -v curl >/dev/null 2>&1; then
    missing+=("curl")
  fi

  if [[ "${#missing[@]}" -eq 0 ]]; then
    return 0
  fi

  echo "==> 自动安装基础依赖: ${missing[*]}"
  install_system_packages "${missing[@]}"
  hash -r

  if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    echo "基础依赖安装失败，请确认 git 和 curl 可用后重试。" >&2
    return 1
  fi
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi

  echo "==> 自动安装 uv，用于补齐 Python 3.11+ 运行时"
  env UV_INSTALL_DIR="$HOME/.local/bin" sh -c "$(curl -LsSf https://astral.sh/uv/install.sh)"
  export PATH="$HOME/.local/bin:$PATH"
  hash -r

  if ! command -v uv >/dev/null 2>&1; then
    echo "uv 安装失败，无法继续自动安装 Python。" >&2
    return 1
  fi
}

ensure_python() {
  PYTHON_BIN="$(find_python || true)"
  if [[ -n "$PYTHON_BIN" ]] && python_version_ok "$PYTHON_BIN"; then
    return 0
  fi

  ensure_uv

  PYTHON_BIN="$(find_uv_python "$(command -v uv)" || true)"
  if [[ -n "$PYTHON_BIN" ]] && python_version_ok "$PYTHON_BIN"; then
    return 0
  fi

  echo "==> 未找到可用的 Python 3.11+，开始自动安装独立 Python 运行时"
  uv python install 3.11
  PYTHON_BIN="$(find_uv_python "$(command -v uv)" || true)"
  if [[ -z "$PYTHON_BIN" ]] || ! python_version_ok "$PYTHON_BIN"; then
    echo "自动安装 Python 3.11+ 失败。" >&2
    return 1
  fi
}

ensure_prereqs() {
  if [[ "$OS_NAME" == "Windows" ]]; then
    echo "检测到当前环境为 Windows shell，建议改用 WSL、Linux 或 macOS 终端运行。" >&2
    exit 1
  fi

  if ! ensure_base_tools || ! ensure_python || ! ensure_uv; then
    python_install_hint
    exit 1
  fi
}

prompt_text() {
  local label="$1"
  local default_value="${2:-}"
  local answer=""

  if [[ -n "$default_value" ]]; then
    printf '%s [%s]: ' "$label" "$default_value" >"$PROMPT_OUTPUT"
  else
    printf '%s: ' "$label" >"$PROMPT_OUTPUT"
  fi

  IFS= read -r answer <"$PROMPT_INPUT" || true
  if [[ -z "$answer" ]]; then
    answer="$default_value"
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
    printf '%s %s: ' "$label" "$prompt" >"$PROMPT_OUTPUT"
    IFS= read -r answer <"$PROMPT_INPUT" || true
    answer="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
    if [[ -z "$answer" ]]; then
      answer="$default_value"
    fi
    case "$answer" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
    esac
    printf '请输入 y 或 n。\n' >"$PROMPT_OUTPUT"
  done
}

run_interactive_guide() {
  printf '==> 当前系统: %s\n' "$OS_NAME" >"$PROMPT_OUTPUT"
  printf '==> 将自动拉取 MoviePilot，并下载前端 release、资源文件与本地 Node 运行时\n' >"$PROMPT_OUTPUT"

  WORKDIR="$(prompt_text "安装目录" "$WORKDIR")"
  APP_DIR_NAME="$(prompt_text "主项目目录名" "$APP_DIR_NAME")"
  CONFIG_DIR="$(prompt_text "配置目录" "$CONFIG_DIR")"

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

detect_rc_file() {
  local shell_name
  shell_name="$(basename "${SHELL:-}")"
  case "$shell_name" in
    zsh)
      printf '%s\n' "$HOME/.zshrc"
      ;;
    bash)
      printf '%s\n' "$HOME/.bashrc"
      ;;
    *)
      printf '%s\n' "$HOME/.profile"
      ;;
  esac
}

ensure_path_configured() {
  if [[ "$LINK_CLI" != "true" ]]; then
    return
  fi

  local bin_dir
  bin_dir="$(dirname "$LINK_PATH")"
  export PATH="$bin_dir:$PATH"

  if [[ "$bin_dir" != "$HOME/.local/bin" ]]; then
    return
  fi

  PATH_RC_FILE="$(detect_rc_file)"
  local export_line='export PATH="$HOME/.local/bin:$PATH"'
  mkdir -p "$(dirname "$PATH_RC_FILE")"
  touch "$PATH_RC_FILE"

  if ! grep -Fqs "$export_line" "$PATH_RC_FILE"; then
    {
      printf '\n# MoviePilot CLI\n'
      printf '%s\n' "$export_line"
    } >>"$PATH_RC_FILE"
    PATH_UPDATED="true"
  fi
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
    --config-dir)
      CONFIG_DIR="$2"
      shift 2
      ;;
    --superuser)
      SUPERUSER="$2"
      shift 2
      ;;
    --superuser-password)
      SUPERUSER_PASSWORD="$2"
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
detect_package_manager
setup_prompt_io
ensure_prereqs
ensure_link_path

if [[ "$NON_INTERACTIVE" != "true" && "$HAS_TTY" == "true" ]]; then
  run_interactive_guide
  ensure_link_path
elif [[ "$RUN_WIZARD" == "true" && "$HAS_TTY" != "true" ]]; then
  echo "==> 未检测到可用终端输入，已跳过初始化向导。安装完成后可手动执行：moviepilot setup --wizard"
  RUN_WIZARD="false"
fi

mkdir -p "$WORKDIR"
WORKDIR="$(cd "$WORKDIR" && pwd)"
APP_DIR="$WORKDIR/$APP_DIR_NAME"
sync_repo

cd "$APP_DIR"
echo "==> 执行本地环境安装与初始化"
SETUP_ARGS=(setup --python "$PYTHON_BIN" --config-dir "$CONFIG_DIR")
if [[ "$RUN_WIZARD" == "true" ]]; then
  SETUP_ARGS+=(--wizard)
fi
if [[ -n "$SUPERUSER" ]]; then
  SETUP_ARGS+=(--superuser "$SUPERUSER")
fi
if [[ -n "$SUPERUSER_PASSWORD" ]]; then
  SETUP_ARGS+=(--superuser-password "$SUPERUSER_PASSWORD")
fi
if [[ "$HAS_TTY" == "true" ]]; then
  "$PYTHON_BIN" ./scripts/local_setup.py "${SETUP_ARGS[@]}" <"$PROMPT_INPUT"
else
  "$PYTHON_BIN" ./scripts/local_setup.py "${SETUP_ARGS[@]}"
fi

if [[ "$LINK_CLI" == "true" ]]; then
  echo "==> 创建全局 moviepilot 命令到 $LINK_PATH"
  ln -sf "$APP_DIR/moviepilot" "$LINK_PATH"
  ensure_path_configured
fi

if [[ "$START_AFTER_INSTALL" == "true" ]]; then
  echo "==> 启动 MoviePilot 前后端服务"
  ./moviepilot start
fi

cat <<EOF
==> 安装完成

系统环境: $OS_NAME
项目目录: $APP_DIR
配置目录: $CONFIG_DIR
Python 解释器: $PYTHON_BIN
CLI 命令: ${LINK_CLI:-false}
CLI 路径: ${LINK_PATH:-未创建}

使用方式:
  moviepilot status
  moviepilot logs --frontend
  moviepilot logs --stdio
  moviepilot config path

完整 CLI 文档:
  $APP_DIR/docs/cli.md
EOF

if [[ "$LINK_CLI" == "true" && "$(dirname "$LINK_PATH")" == "$HOME/.local/bin" ]]; then
  echo
  echo "PATH 说明:"
  if [[ "$PATH_UPDATED" == "true" ]]; then
    echo "  已将 ~/.local/bin 写入 $PATH_RC_FILE"
  fi
  echo "  如果当前终端仍提示找不到 moviepilot，请重新打开终端，或执行："
  echo "  source ${PATH_RC_FILE:-$HOME/.profile}"
fi
