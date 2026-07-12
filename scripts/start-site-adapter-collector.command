#!/usr/bin/env bash

set -u

# 从解压目录启动 macOS 单文件采集器，并在结束后保留终端窗口供用户查看结果。
main() {
  local script_dir=""
  local collector_path=""
  local status=0

  script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  collector_path="$script_dir/moviepilot-site-collector-macos"
  if [[ ! -f "$collector_path" ]]; then
    echo "未找到 moviepilot-site-collector-macos，请完整解压 ZIP 后再双击启动。" >&2
    status=1
  else
    chmod +x "$collector_path"
    cd "$script_dir" || status=1
    if [[ "$status" -eq 0 ]]; then
      "$collector_path" || status=$?
    fi
  fi

  echo
  read -r -p "按回车关闭窗口..." _
  exit "$status"
}

main "$@"
