#!/bin/bash
# shellcheck shell=bash
# shellcheck disable=SC2086
# shellcheck disable=SC2144

Green="\033[32m"
Red="\033[31m"
Yellow='\033[33m'
Font="\033[0m"
INFO="[${Green}INFO${Font}]"
ERROR="[${Red}ERROR${Font}]"
WARN="[${Yellow}WARN${Font}]"
function INFO() {
    echo -e "${INFO} ${1}"
}
function ERROR() {
    echo -e "${ERROR} ${1}"
}
function WARN() {
    echo -e "${WARN} ${1}"
}

# 设置虚拟环境路径（兼容群晖等系统必须这样配置）
VENV_PATH="${VENV_PATH:-/opt/venv}"
export PATH="${VENV_PATH}/bin:$PATH"

CONFIG_DIR="${CONFIG_DIR:-/config}"

function apply_package_cache_env() {
    PACKAGE_CACHE_ROOT="${PACKAGE_CACHE_ROOT:-${CONFIG_DIR}/.cache}"
    export PACKAGE_CACHE_ROOT
    export PIP_CACHE_DIR="${PIP_CACHE_DIR:-${PACKAGE_CACHE_ROOT}/pip}"
    export UV_CACHE_DIR="${UV_CACHE_DIR:-${PACKAGE_CACHE_ROOT}/uv}"
    mkdir -p "${PIP_CACHE_DIR}" "${UV_CACHE_DIR}"
}

apply_package_cache_env

PIP_ENV=()
MOVIEPILOT_UPDATE_RESULT="noop"

function set_package_proxy_env() {
    PIP_ENV=()
    if [[ -n "${PROXY_HOST}" ]]; then
        PIP_ENV=(
            "HTTP_PROXY=${PROXY_HOST}"
            "HTTPS_PROXY=${PROXY_HOST}"
            "http_proxy=${PROXY_HOST}"
            "https_proxy=${PROXY_HOST}"
        )
    fi
}

# 下载及解压
function download_and_unzip() {
    local retries=0
    local max_retries=3
    local url="$1"
    local target_dir="$2"
    INFO "→ 正在下载 ${url}..."
    while [ $retries -lt $max_retries ]; do
        if curl ${CURL_OPTIONS} "${url}" ${CURL_HEADERS} | busybox unzip -d ${TMP_PATH} - > /dev/null; then
            if [ -e ${TMP_PATH}/MoviePilot-* ]; then
                mv ${TMP_PATH}/MoviePilot-* ${TMP_PATH}/"${target_dir}"
            fi
            break
        else
            WARN "下载 ${url} 失败，正在进行第 $((retries + 1)) 次重试..."
            retries=$((retries + 1))
        fi
    done
    if [ $retries -eq $max_retries ]; then
        ERROR "下载 ${url} 失败，已达到最大重试次数！"
        return 1
    else
        return 0
    fi
}

# 下载程序资源，$1: 后端版本路径
function install_backend_and_download_resources() {
    # 更新后端程序
    if ! download_and_unzip "${GITHUB_PROXY}https://github.com/jxxghp/MoviePilot/archive/refs/${1}" "App"; then
        WARN "后端程序下载失败，继续使用旧的程序来启动..."
        return 1
    fi
    INFO "后端程序下载成功"
    
    # 检查依赖是否有变化
    INFO "→ 检查依赖变化..."
    if [ -f "${TMP_PATH}/App/requirements.in" ]; then
        if ! cmp -s /app/requirements.in "${TMP_PATH}/App/requirements.in"; then
            INFO "检测到依赖变化，正在更新虚拟环境..."
            configure_pip_route
            INFO "PIP：${PIP_LOG}"
            local compiled_requirements="${TMP_PATH}/requirements.txt"
            if ! env "${PIP_ENV[@]}" ${VENV_PATH}/bin/pip-compile \
                "${TMP_PATH}/App/requirements.in" -o "${compiled_requirements}"; then
                ERROR "依赖编译失败，当前程序依赖未变更"
                return 1
            fi
            if ! env "${PIP_ENV[@]}" ${VENV_PATH}/bin/pip install ${PIP_OPTIONS} \
                -r "${compiled_requirements}"; then
                ERROR "依赖安装失败，当前程序依赖清单未变更"
                return 1
            fi
            INFO "依赖更新成功"
        else
            INFO "依赖无变化，跳过依赖更新"
        fi
    else
        WARN "未找到requirements.in文件，跳过依赖检查"
    fi
    
    # 如果是"heads/v3.zip"，则查找v3开头的最新版本号
    if [[ "${1}" == "heads/v3.zip" ]]; then
        INFO "→ 正在获取前端最新版本号..."
        # 获取所有发布的版本列表，并筛选出以v3开头的版本号
        releases=$(curl ${CURL_OPTIONS} "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases" ${CURL_HEADERS} | jq -r '.[].tag_name' | grep "^v3\.")
        if [ -z "$releases" ]; then
            WARN "未找到任何v3前端版本，继续启动..."
            return 1
        else
            # 找到最新的v3版本
            frontend_version=$(echo "$releases" | sort -V | tail -n 1)
        fi
        INFO "前端最新版本号：${frontend_version}"
    else
        INFO "→ 正在获取前端版本号..."
        # 从后端文件中读取前端版本号
        frontend_version=$(sed -n "s/^FRONTEND_VERSION\s*=\s*'\([^']*\)'/\1/p" ${TMP_PATH}/App/version.py)
        if [[ "${frontend_version}" != *v* ]]; then
            WARN "前端版本号获取失败，继续启动..."
            return 1
        fi
        INFO "前端版本号：${frontend_version}"
    fi
    # 更新前端程序
    if ! download_and_unzip "${GITHUB_PROXY}https://github.com/jxxghp/MoviePilot-Frontend/releases/download/${frontend_version}/dist.zip" "dist"; then
        WARN "前端程序下载失败，继续使用旧的程序来启动..."
        return 1
    fi
    INFO "前端程序下载成功"
    # 备份插件目录
    INFO "→ 正在备份插件目录..."
    if ! rm -rf /plugins \
        || ! mkdir -p /plugins \
        || ! cp -a /app/app/plugins/* /plugins/; then
        ERROR "插件目录备份失败，终止更新"
        return 1
    fi
    rm -f /plugins/__init__.py
    # 备份站点资源
    INFO "→ 正在备份站点资源目录..."
    if ! rm -rf /resources_bakcup || ! mkdir /resources_bakcup; then
        ERROR "站点资源备份目录准备失败，终止更新"
        return 1
    fi
    resource_source_dir=/app/app/application/site
    for legacy_resource_dir in /app/app/infrastructure /app/app/adapters/network /app/app/helper; do
        if [ ! -d "${resource_source_dir}" ] && [ -d "${legacy_resource_dir}" ]; then
            # 升级时允许读取历史目录，恢复目标始终使用 canonical 站点应用目录。
            resource_source_dir="${legacy_resource_dir}"
        fi
    done
    if [ -f "${resource_source_dir}/user.sites.v3.bin" ]; then
        cp -a "${resource_source_dir}/user.sites.v3.bin" /resources_bakcup
    fi
    for resource_file in "${resource_source_dir}"/sites.cp*; do
        [ -f "${resource_file}" ] && cp -a "${resource_file}" /resources_bakcup
    done
    # 清空程序目录
    if ! rm -rf /app \
        || ! mkdir -p /app \
        || ! cp -a ${TMP_PATH}/App/* /app/ \
        || ! rm -rf /public \
        || ! mkdir -p /public \
        || ! cp -a ${TMP_PATH}/dist/* /public/; then
        ERROR "程序文件替换失败，更新未完成"
        return 1
    fi
    INFO "程序部分更新成功，前端版本：${frontend_version}，后端版本：${1}"
    # 恢复插件目录
    if ! cp -a /plugins/* /app/app/plugins/; then
        ERROR "插件目录恢复失败，更新未完成"
        return 1
    fi
    # 更新站点资源
    INFO "→ 开始更新站点资源..."
    python_version=$(python3 -c 'import sys; print(f"cpython-{sys.version_info.major}{sys.version_info.minor}")')
    arch=$(uname -m)
    if [ "$arch" = "aarch64" ]; then
        arch_suffix="aarch64-linux-gnu"
    else
        arch_suffix="x86_64-linux-gnu"
    fi
    INFO "当前 Python 版本：${python_version}，架构：${arch}"
    if ! mkdir -p /app/app/application/site; then
        ERROR "站点资源目录创建失败，更新未完成"
        return 1
    fi
    # 下载 V3 站点索引
    if ! curl ${CURL_OPTIONS} "${GITHUB_PROXY}https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/resources.v3/user.sites.v3.bin" -o /app/app/application/site/user.sites.v3.bin; then
        if [ -f /resources_bakcup/user.sites.v3.bin ]; then
            cp -a /resources_bakcup/user.sites.v3.bin /app/app/application/site/
        fi
        WARN "user.sites.v3.bin 下载失败，继续使用旧的资源来启动..."
    fi
    # 下载对应平台的 sites 文件
    sites_file="sites.${python_version}-${arch_suffix}.so"
    if ! curl ${CURL_OPTIONS} "${GITHUB_PROXY}https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/resources.v3/${sites_file}" -o "/app/app/application/site/${sites_file}"; then
        if [ -f "/resources_bakcup/${sites_file}" ]; then
            cp -a "/resources_bakcup/${sites_file}" /app/app/application/site/
        fi
        WARN "${sites_file} 下载失败，继续使用旧的资源来启动..."
    fi
    INFO "站点资源更新成功"
    # 清理临时目录
    rm -rf "${TMP_PATH}"
    MOVIEPILOT_UPDATE_RESULT="updated"
    return 0
}

function probe_pip_package() {
    local probe_env=(
        "UV_NO_CACHE=1"
        "PIP_NO_CACHE_DIR=1"
        "UV_HTTP_TIMEOUT=5"
        "PIP_DEFAULT_TIMEOUT=5"
        "UV_HTTP_RETRIES=0"
        "PIP_RETRIES=0"
    )
    local package_index="${1:-}"
    local use_proxy="${2:-false}"
    local probe_dir
    local -a probe_args=(install)

    if [[ "${use_proxy}" = "true" ]]; then
        probe_env+=(
            "HTTP_PROXY=${PROXY_HOST}"
            "HTTPS_PROXY=${PROXY_HOST}"
            "http_proxy=${PROXY_HOST}"
            "https_proxy=${PROXY_HOST}"
        )
    fi
    probe_dir=$(mktemp -d) || return 1
    # 包源探针必须使用独立目标目录，避免修改主程序与插件共享的虚拟环境。
    probe_args+=(--target "${probe_dir}" --no-deps)
    if [[ -n "${package_index}" ]]; then
        probe_args+=(-i "${package_index}")
    fi
    probe_args+=(pip-hello-world)

    (
        trap 'rm -rf "${probe_dir}"' EXIT
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM
        timeout --kill-after=2s 10s env "${probe_env[@]}" \
            "${VENV_PATH}/bin/pip" "${probe_args[@]}" > /dev/null 2>&1
    )
}

function test_connectivity_pip() {
    case "$1" in
    0)
        if [[ -n "${PIP_PROXY}" ]]; then
            if [[ -n "${PROXY_HOST}" ]]; then
                probe_pip_package "${PIP_PROXY}" true
            else
                probe_pip_package "${PIP_PROXY}" false
            fi
            if [[ $? -eq 0 ]]; then
                PIP_OPTIONS="-i ${PIP_PROXY}"
                PIP_LOG="镜像代理模式"
                set_package_proxy_env
                return 0
            fi
        fi
        return 1
        ;;
    1)
        if [[ -n "${PROXY_HOST}" ]]; then
            if probe_pip_package "" true; then
                PIP_OPTIONS=""
                PIP_LOG="全局代理模式"
                set_package_proxy_env
                return 0
            fi
        fi
        return 1
        ;;
    2)
        PIP_ENV=()
        PIP_OPTIONS=""
        PIP_LOG="不使用代理"
        return 0
        ;;
    esac
}

# 测试Github连通性
function test_connectivity_github() {
    case "$1" in
    0)
        if [[ -n "${GITHUB_PROXY}" ]]; then
            if curl -sL --connect-timeout 5 --max-time 10 "${GITHUB_PROXY}https://raw.githubusercontent.com/jxxghp/MoviePilot/main/README.md" > /dev/null 2>&1; then
                GITHUB_LOG="镜像代理模式"
                return 0
            fi
        fi
        return 1
        ;;
    1)
        if [[ -n "${PROXY_HOST}" ]]; then
            if curl -sL --connect-timeout 5 --max-time 10 -x ${PROXY_HOST} https://raw.githubusercontent.com/jxxghp/MoviePilot/main/README.md > /dev/null 2>&1; then
                CURL_OPTIONS="-sL -x ${PROXY_HOST}"
                GITHUB_LOG="全局代理模式"
                return 0
            fi
        fi
        return 1
        ;;
    2)
        CURL_OPTIONS="-sL"
        GITHUB_LOG="不使用代理"
        return 0
        ;;
    esac
}

function configure_pip_route() {
    local retries=0
    while true; do
        if test_connectivity_pip "${retries}"; then
            return 0
        fi
        retries=$((retries + 1))
    done
}

function fetch_latest_v3_release() {
    local response
    local releases
    local latest_release

    response=$(curl ${CURL_OPTIONS} --compressed --fail --connect-timeout 5 --max-time 15 \
        "https://api.github.com/repos/jxxghp/MoviePilot/releases" \
        ${CURL_HEADERS}) || return 1
    releases=$(printf '%s\n' "${response}" | jq -r '.[].tag_name') || return 1
    latest_release=$(printf '%s\n' "${releases}" | grep "^v3\." | sort -V | tail -n 1)
    [[ -n "${latest_release}" ]] || return 1
    printf '%s\n' "${latest_release}"
}

# 版本号比较
function compare_versions() {
    local v1="$1"
    local v2="$2"
    # 去掉开头的 v 或 V
    v1="${v1#[vV]}"
    v2="${v2#[vV]}"
    local current_ver_parts=()
    local release_ver_parts=()
    IFS='.-' read -ra current_ver_parts <<< "$v1"
    IFS='.-' read -ra release_ver_parts <<< "$v2"
    local i
    local current_ver
    local release_ver

    for ((i = 0; i < ${#current_ver_parts[@]} || i < ${#release_ver_parts[@]}; i++)); do
        # 版本号不足位补 0
        local current_ver_part="${current_ver_parts[i]:-0}"
        local release_ver_part="${release_ver_parts[i]:-0}"
        current_ver=$(get_priority "$current_ver_part")
        release_ver=$(get_priority "$release_ver_part")

        # 任意一个为-5，不在合法版本号内，无法比较
        if (( current_ver == -5 || release_ver == -5 )); then
            ERROR "存在不合法版本号，无法判断，跳过更新步骤..."
            return 1
        else
            if (( current_ver > release_ver )); then
                WARN "当前版本高于远程版本，跳过更新步骤..."
                return 1
            elif (( current_ver < release_ver )); then
                INFO "发现新版本，开始自动升级..."
                if install_backend_and_download_resources "tags/$2.zip"; then
                    return 0
                fi
                MOVIEPILOT_UPDATE_RESULT="failed"
                return 1
            else
                continue
            fi
        fi
    done
    WARN "当前版本已是最新版本，跳过更新步骤..."
}

# 优先级转换
function get_priority() {
    local version="$1"
    if [[ $version =~ ^[0-9]+$ ]]; then
        echo $version
    else
        case $version in
            "stable")
                echo -1
                ;;
            "rc")
                echo -2
                ;;
            "beta")
                echo -3
                ;;
            "alpha")
                echo -4
                ;;
            # 非数字的不合法版本号
            *)
                echo -5
                ;;
        esac
    fi
}

function run_moviepilot_update() {
MOVIEPILOT_UPDATE_RESULT="noop"
if [[ "${MOVIEPILOT_AUTO_UPDATE}" = "true" ]] || [[ "${MOVIEPILOT_AUTO_UPDATE}" = "release" ]] || [[ "${MOVIEPILOT_AUTO_UPDATE}" = "dev" ]]; then
    TMP_PATH=$(mktemp -d)
    if [ ! -d "${TMP_PATH}" ]; then
        # 如果自动生成 tmp 文件夹失败则手动指定，避免出现数据丢失等情况
        TMP_PATH=/tmp/mp_update_path
        if [ -d /tmp/mp_update_path ]; then
            rm -rf /tmp/mp_update_path
        fi
        mkdir -p /tmp/mp_update_path
    fi
    retries=0
    while true; do
        if test_connectivity_github ${retries}; then
            break
        else
            retries=$((retries + 1))
        fi
    done
    INFO "Github：${GITHUB_LOG}"
    if [ -n "${GITHUB_TOKEN}" ]; then
        CURL_HEADERS="--oauth2-bearer ${GITHUB_TOKEN}"
    else
        CURL_HEADERS=""
    fi
    if [ "${MOVIEPILOT_AUTO_UPDATE}" = "dev" ]; then
        INFO "Dev 更新模式"
        if ! install_backend_and_download_resources "heads/v3.zip"; then
            MOVIEPILOT_UPDATE_RESULT="failed"
        fi
    else
        INFO "Release 更新模式"
        old_version=$(grep -m -1 "^\s*APP_VERSION\s*=\s*" /app/version.py | tr -d '\r\n' | awk -F'#' '{print $1}' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
        if [[ "${old_version}" == *APP_VERSION* ]]; then
            current_version=$(echo "${old_version}" | sed -rn "s/APP_VERSION\s*=\s*['\"](.*)['\"]/\1/gp")
            INFO "当前版本号：${current_version}"
            if ! latest_v3=$(fetch_latest_v3_release); then
                WARN "未找到任何v3后端版本，继续启动..."
            else
                INFO "最新的v3后端版本号：${latest_v3}"
                # 使用版本号比较函数进行比较，并下载最新版本
                compare_versions "${current_version}" "${latest_v3}"
            fi
        else
            WARN "当前版本号获取失败，继续启动..."
        fi
    fi
    if [ -d "${TMP_PATH}" ]; then
        rm -rf "${TMP_PATH}"
    fi
elif [[ "${MOVIEPILOT_AUTO_UPDATE}" = "false" ]]; then
    INFO "程序自动升级已关闭，如需自动升级请在创建容器时设置环境变量：MOVIEPILOT_AUTO_UPDATE=release"
else
    INFO "MOVIEPILOT_AUTO_UPDATE 变量设置错误"
fi
}
