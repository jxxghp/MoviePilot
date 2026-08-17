#!/bin/bash
# shellcheck shell=bash

# 浏览器缓存可由容器环境显式覆盖；未覆盖时优先复用已存在的持久缓存。
function is_cloakbrowser_cache_ready() {
    local candidate="${1:-}"
    [ -d "${candidate}" ] || return 1

    CLOAKBROWSER_CACHE_DIR="${candidate}" "${VENV_PATH}/bin/python3" -c '
import os
from cloakbrowser import binary_info

info = binary_info()
path = info.get("binary_path")
raise SystemExit(0 if info.get("installed") and path and os.access(path, os.X_OK) else 1)
' >/dev/null 2>&1
}

function is_path_mountpoint() {
    local candidate="${1:-}"
    "${VENV_PATH}/bin/python3" -c '
import os
import sys

raise SystemExit(0 if os.path.ismount(sys.argv[1]) else 1)
' "${candidate}" >/dev/null 2>&1
}

function resolve_browser_cache_dir() {
    local explicit_cache="${CLOAKBROWSER_CACHE_DIR:-}"
    local browser_cache_root="${CONFIG_DIR}/.browser"
    local config_cache="${browser_cache_root}/cloakbrowser"
    local legacy_cache="${HOME}/.cloakbrowser"
    local selected_cache
    local selected_source

    if [ -n "${explicit_cache}" ]; then
        if [[ "${explicit_cache}" != /* ]]; then
            ERROR "→ CLOAKBROWSER_CACHE_DIR 必须是独立的绝对目录：${explicit_cache}"
            return 1
        fi
        explicit_cache="$(python3 -c 'import os, sys; print(os.path.normpath(sys.argv[1]))' "${explicit_cache}")"
        if [ "${explicit_cache}" = "/" ] \
            || [ "${explicit_cache}" = "${CONFIG_DIR%/}" ] \
            || [ "${explicit_cache}" = "${HOME%/}" ]; then
            ERROR "→ CLOAKBROWSER_CACHE_DIR 不能占用受管根目录：${explicit_cache}"
            return 1
        fi
        selected_cache="${explicit_cache}"
        selected_source="容器环境"
    elif is_cloakbrowser_cache_ready "${config_cache}"; then
        selected_cache="${config_cache}"
        selected_source="V3 持久缓存"
    elif is_cloakbrowser_cache_ready "${legacy_cache}" || is_path_mountpoint "${legacy_cache}"; then
        selected_cache="${legacy_cache}"
        selected_source="预发布 V3 兼容缓存"
    else
        selected_cache="${config_cache}"
        selected_source="新安装默认目录"
    fi

    CLOAKBROWSER_CACHE_DIR="${selected_cache}"
    export CLOAKBROWSER_CACHE_DIR
    INFO "→ CloakBrowser 缓存目录：${CLOAKBROWSER_CACHE_DIR}（${selected_source}）"
}

# 权限修复跳过大体积缓存子树，仅处理其根目录和直接子项。
function chown_path_excluding_browser_cache() {
    local target="${1:-}"
    local cache_dir="${CLOAKBROWSER_CACHE_DIR:-}"

    [ -e "${target}" ] || return 0
    if [ -n "${cache_dir}" ] && [ "${cache_dir}" = "${target}" ]; then
        chown -h moviepilot:moviepilot "${target}"
        return 0
    fi
    if [ -n "${cache_dir}" ] && [[ "${cache_dir}" == "${target}"/* ]]; then
        find "${target}" -path "${cache_dir}" -prune -o -exec chown -h moviepilot:moviepilot {} +
        return 0
    fi
    chown -R moviepilot:moviepilot "${target}"
}

function prepare_browser_cache_dir() {
    if ! mkdir -p "${CLOAKBROWSER_CACHE_DIR}"; then
        ERROR "→ 无法创建 CloakBrowser 缓存目录：${CLOAKBROWSER_CACHE_DIR}"
        return 1
    fi

    if [ "${CLOAKBROWSER_CACHE_DIR}" = "${CONFIG_DIR}/.browser/cloakbrowser" ]; then
        chown -h moviepilot:moviepilot "${CONFIG_DIR}/.browser"
    fi
    chown -h moviepilot:moviepilot "${CLOAKBROWSER_CACHE_DIR}"
    find "${CLOAKBROWSER_CACHE_DIR}" -mindepth 1 -maxdepth 1 -exec chown -h moviepilot:moviepilot {} +
    if is_truthy_value "${MOVIEPILOT_FORCE_CHOWN:-false}"; then
        chown -R moviepilot:moviepilot "${CLOAKBROWSER_CACHE_DIR}"
    fi

    if ! gosu moviepilot:moviepilot sh -c '[ -r "$1" ] && [ -w "$1" ] && [ -x "$1" ]' sh "${CLOAKBROWSER_CACHE_DIR}"; then
        ERROR "→ CloakBrowser 缓存目录不可读写：${CLOAKBROWSER_CACHE_DIR}"
        return 1
    fi
}

function install_browser_kernel() {
    local emulation="${BROWSER_EMULATION:-cloakbrowser}"
    emulation="$(normalize_env_value "${emulation}")"
    local proxy="${HTTPS_PROXY:-${https_proxy:-${PROXY_HOST:-}}}"

    if [ "${emulation}" != "cloakbrowser" ] && [ "${emulation}" != "flaresolverr" ] && [ -n "${emulation}" ]; then
        WARN "浏览器仿真类型 ${emulation} 已按 CloakBrowser 处理。"
    fi

    INFO "确保 CloakBrowser 浏览器内核可用"
    if [[ "$proxy" =~ ^https?:// ]]; then
        HTTPS_PROXY="$proxy" gosu moviepilot:moviepilot "${VENV_PATH}/bin/python3" -m cloakbrowser install
    else
        gosu moviepilot:moviepilot "${VENV_PATH}/bin/python3" -m cloakbrowser install
    fi
}

function ensure_browser_kernel() {
    if is_cloakbrowser_cache_ready "${CLOAKBROWSER_CACHE_DIR:-}"; then
        INFO "CloakBrowser 浏览器内核已就绪"
        return 0
    fi

    if ! install_browser_kernel; then
        WARN "CloakBrowser 浏览器内核安装失败，首次使用时将重试"
    fi
}
