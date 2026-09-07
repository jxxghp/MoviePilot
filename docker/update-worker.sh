#!/bin/bash
# shellcheck shell=bash

set -u

VENV_PATH="${VENV_PATH:-/opt/venv}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
export VENV_PATH CONFIG_DIR
SUPERVISOR_CONFIG="/etc/supervisor/supervisord.conf"
RESTART_REQUEST_FILE="${CONFIG_DIR}/temp/moviepilot.pending_supervisor_restart"

function INFO() {
    echo "[INFO] ${1}"
}

function ERROR() {
    echo "[ERROR] ${1}" >&2
}

cd /app || exit 1

INFO "→ 开始将已下载的更新包替换到 Docker 程序目录..."
if ! "${VENV_PATH}/bin/python3" -m app.cli apply-prepared-update; then
    ERROR "→ Docker 更新包替换失败，保留当前运行进程。"
    exit 1
fi

if ! mkdir -p "$(dirname "${RESTART_REQUEST_FILE}")"; then
    ERROR "→ 无法记录更新后的重启请求。"
    exit 1
fi
restart_request_tmp="${RESTART_REQUEST_FILE}.tmp.$$"
if ! printf '%s\n' update > "${restart_request_tmp}" \
    || ! mv -f "${restart_request_tmp}" "${RESTART_REQUEST_FILE}"; then
    rm -f "${restart_request_tmp}"
    ERROR "→ 无法记录更新后的重启请求。"
    exit 1
fi

INFO "→ 更新包已替换，通知 supervisor 关闭并由容器入口重新启动新代码..."
if ! /usr/bin/supervisorctl -c "${SUPERVISOR_CONFIG}" shutdown; then
    ERROR "→ supervisor 关闭请求失败，更新代码已落盘，可手动重启后生效。"
    exit 1
fi
