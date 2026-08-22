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
UV_BIN="${UV_BIN:-/usr/local/bin/uv}"

CONFIG_DIR="${CONFIG_DIR:-/config}"
APP_DIR=/app
PUBLIC_DIR=/public
UPDATE_PENDING_FILE="${CONFIG_DIR}/temp/__update_pending__"
UPDATE_PREVIOUS_APP="${APP_DIR}.__update_previous__"
UPDATE_PREVIOUS_PUBLIC="${PUBLIC_DIR}.__update_previous__"

function apply_package_cache_env() {
    PACKAGE_CACHE_ROOT="${PACKAGE_CACHE_ROOT:-${CONFIG_DIR}/.cache}"
    export PACKAGE_CACHE_ROOT
    export UV_CACHE_DIR="${UV_CACHE_DIR:-${PACKAGE_CACHE_ROOT}/uv}"
    mkdir -p "${UV_CACHE_DIR}"
}

apply_package_cache_env

PACKAGE_ENV=()
UV_OPTIONS=()
MOVIEPILOT_UPDATE_RESULT="noop"
UPDATE_RECOVERY_REQUIRED="false"
UPDATE_RECOVERY_COMPLETED="false"
DEPENDENCY_SYNC_ATTEMPTED="false"
PACKAGE_ROUTE_READY="false"

function set_package_proxy_env() {
    PACKAGE_ENV=()
    if [[ -n "${PROXY_HOST}" ]]; then
        PACKAGE_ENV=(
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

function sync_project_dependencies() {
    INFO "检测到依赖变化，正在更新虚拟环境..."
    configure_package_route || return 1
    PACKAGE_ROUTE_READY="true"
    DEPENDENCY_SYNC_ATTEMPTED="true"
    INFO "依赖源：${PACKAGE_LOG}"
    if ! sync_project_dependencies_for "${TMP_PATH}/App"; then
        ERROR "依赖同步失败，当前程序依赖未完成更新"
        return 1
    fi
    INFO "依赖更新成功"
}

function dependency_manifests_changed() {
    ! cmp -s "${APP_DIR}/pyproject.toml" "${TMP_PATH}/App/pyproject.toml" \
        || ! cmp -s "${APP_DIR}/uv.lock" "${TMP_PATH}/App/uv.lock"
}

function set_update_pending() {
    local state="${1:-prepared}"
    local pending_dir
    local pending_tmp

    pending_dir="$(dirname "${UPDATE_PENDING_FILE}")"
    pending_tmp="${pending_dir}/.__update_pending__.tmp.$$"
    mkdir -p "${pending_dir}" \
        && printf '%s\n' "${state}" > "${pending_tmp}" \
        && mv -f "${pending_tmp}" "${UPDATE_PENDING_FILE}" || {
        rm -f "${pending_tmp}"
        return 1
    }
}

function clear_update_pending() {
    rm -f "${UPDATE_PENDING_FILE}"
}

function update_pending_state() {
    [ -f "${UPDATE_PENDING_FILE}" ] || return 1
    tr -d '\r\n' < "${UPDATE_PENDING_FILE}"
}

function sync_project_dependencies_for() {
    local project_dir="$1"
    local -a uv_cmd=(
        "${UV_BIN}" sync
        --project "${project_dir}"
        --locked
        --inexact
        --no-dev
        --no-install-project
        --python "${VENV_PATH}/bin/python3"
    )
    uv_cmd+=("${UV_OPTIONS[@]}")
    env "${PACKAGE_ENV[@]}" \
        "UV_PROJECT_ENVIRONMENT=${VENV_PATH}" \
        "UV_LINK_MODE=copy" "${uv_cmd[@]}"
}

function restore_project_dependencies() {
    if [ "${PACKAGE_ROUTE_READY}" != "true" ]; then
        configure_package_route || return 1
        PACKAGE_ROUTE_READY="true"
    fi
    INFO "→ 正在恢复更新前的程序依赖..."
    if ! sync_project_dependencies_for "${APP_DIR}"; then
        ERROR "依赖回滚失败，保留更新事务标记以便下次启动继续恢复"
        return 1
    fi
    INFO "→ 更新前的程序依赖已恢复"
}

function cleanup_previous_payload() {
    rm -rf "${UPDATE_PREVIOUS_APP}" "${UPDATE_PREVIOUS_PUBLIC}"
}

function finalize_update_transaction() {
    cleanup_previous_payload || return 1
    clear_update_pending
}

function restore_previous_payload() {
    local failed="false"

    if [ -e "${UPDATE_PREVIOUS_APP}" ]; then
        if [ -e "${APP_DIR}" ] && ! rm -rf "${APP_DIR}"; then
            failed="true"
        elif ! mv "${UPDATE_PREVIOUS_APP}" "${APP_DIR}"; then
            failed="true"
        fi
    fi
    if [ -e "${UPDATE_PREVIOUS_PUBLIC}" ]; then
        if [ -e "${PUBLIC_DIR}" ] && ! rm -rf "${PUBLIC_DIR}"; then
            failed="true"
        elif ! mv "${UPDATE_PREVIOUS_PUBLIC}" "${PUBLIC_DIR}"; then
            failed="true"
        fi
    fi
    [ "${failed}" = "false" ]
}

function rollback_update_transaction() {
    local failed="false"

    if ! restore_previous_payload; then
        failed="true"
    fi
    if [ "${DEPENDENCY_SYNC_ATTEMPTED}" = "true" ] && ! restore_project_dependencies; then
        failed="true"
    fi

    if [ "${failed}" = "true" ]; then
        UPDATE_RECOVERY_REQUIRED="true"
        return 1
    fi
    if ! finalize_update_transaction; then
        UPDATE_RECOVERY_REQUIRED="true"
        return 1
    fi
    return 0
}

function recover_pending_update() {
    local state
    state="$(update_pending_state 2>/dev/null || true)"
    [ -n "${state}" ] || return 0

    if [ "${state}" = "committed" ]; then
        INFO "→ 清理已完成的容器更新事务"
        if ! finalize_update_transaction; then
            WARN "→ 已完成更新的旧代际备份清理失败，保留事务标记以便下次启动重试"
            return 1
        fi
        return 0
    fi

    WARN "→ 检测到未完成的容器更新事务，正在恢复旧版本"
    if [ "${state}" = "dependencies" ]; then
        DEPENDENCY_SYNC_ATTEMPTED="true"
    fi
    rollback_update_transaction || return 1
    UPDATE_RECOVERY_COMPLETED="true"
    INFO "→ 未完成的容器更新事务已恢复"
}

function existing_resource_dir() {
    local resource_source_dir="${APP_DIR}/app/application/site"
    for legacy_resource_dir in "${APP_DIR}/app/infrastructure" "${APP_DIR}/app/adapters/network" "${APP_DIR}/app/helper"; do
        if [ ! -d "${resource_source_dir}" ] && [ -d "${legacy_resource_dir}" ]; then
            resource_source_dir="${legacy_resource_dir}"
        fi
    done
    printf '%s\n' "${resource_source_dir}"
}

function download_staged_resource() {
    local url="$1"
    local destination="$2"
    local fallback="$3"
    local label="$4"

    if curl ${CURL_OPTIONS} --fail "${url}" -o "${destination}" \
        && [ -s "${destination}" ]; then
        return 0
    fi
    rm -f "${destination}"
    if [ -f "${fallback}" ]; then
        cp -a "${fallback}" "${destination}"
        return $?
    fi
    ERROR "${label} 下载失败且没有可用旧资源"
    return 1
}

function stage_runtime_payload() {
    local stage_app="${TMP_PATH}/App"
    local stage_plugin_dir="${stage_app}/app/plugins"
    local stage_resource_dir="${stage_app}/app/application/site"
    local resource_source_dir
    local resource_file
    local python_version
    local arch
    local arch_suffix
    local sites_file

    [ -f "${stage_app}/version.py" ] || return 1
    [ -f "${stage_app}/pyproject.toml" ] || return 1
    [ -f "${stage_app}/uv.lock" ] || return 1
    [ -f "${TMP_PATH}/dist/index.html" ] || return 1

    # app/plugins 是纯数据目录：里面只有扩展，没有宿主源码。升级会把整个 /app 换代，
    # 所以用户已装的扩展要先搬进新代际的同名目录；新版本自带的扩展留在原地不清空，
    # 同名时以运行目录里的实际安装为准。
    mkdir -p "${stage_plugin_dir}" || return 1
    if [ -d "${APP_DIR}/app/plugins" ]; then
        cp -a "${APP_DIR}/app/plugins/." "${stage_plugin_dir}/" || return 1
    fi

    resource_source_dir="$(existing_resource_dir)"
    mkdir -p "${stage_resource_dir}" || return 1
    if [ -f "${resource_source_dir}/user.sites.v3.bin" ] \
        && ! cp -a "${resource_source_dir}/user.sites.v3.bin" "${stage_resource_dir}/"; then
        return 1
    fi
    for resource_file in "${resource_source_dir}"/sites.cp*; do
        [ -f "${resource_file}" ] || continue
        cp -a "${resource_file}" "${stage_resource_dir}/" || return 1
    done

    python_version="$(python3 -c 'import sys; print(f"cpython-{sys.version_info.major}{sys.version_info.minor}")')" || return 1
    arch="$(uname -m)"
    if [ "${arch}" = "aarch64" ]; then
        arch_suffix="aarch64-linux-gnu"
    else
        arch_suffix="x86_64-linux-gnu"
    fi
    sites_file="sites.${python_version}-${arch_suffix}.so"
    download_staged_resource \
        "${GITHUB_PROXY}https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/resources.v3/user.sites.v3.bin" \
        "${stage_resource_dir}/user.sites.v3.bin" \
        "${resource_source_dir}/user.sites.v3.bin" \
        "user.sites.v3.bin" || return 1
    download_staged_resource \
        "${GITHUB_PROXY}https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/resources.v3/${sites_file}" \
        "${stage_resource_dir}/${sites_file}" \
        "${resource_source_dir}/${sites_file}" \
        "${sites_file}" || return 1
}

function swap_staged_payload() {
    cleanup_previous_payload || return 1
    mv "${APP_DIR}" "${UPDATE_PREVIOUS_APP}" || return 1
    if ! mv "${PUBLIC_DIR}" "${UPDATE_PREVIOUS_PUBLIC}"; then
        mv "${UPDATE_PREVIOUS_APP}" "${APP_DIR}" || true
        return 1
    fi
    if ! mv "${TMP_PATH}/App" "${APP_DIR}" || ! mv "${TMP_PATH}/dist" "${PUBLIC_DIR}"; then
        restore_previous_payload || true
        return 1
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
    
    # 检查依赖清单，实际同步延后到所有运行载荷准备完成之后。
    INFO "→ 检查依赖变化..."
    local dependencies_changed="false"
    if [ -f "${TMP_PATH}/App/pyproject.toml" ] && [ -f "${TMP_PATH}/App/uv.lock" ]; then
        if dependency_manifests_changed; then
            dependencies_changed="true"
        else
            INFO "依赖无变化，跳过依赖更新"
        fi
    else
        ERROR "更新包缺少 pyproject.toml 或 uv.lock，拒绝替换当前程序"
        return 1
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
    INFO "→ 正在准备插件和站点资源..."
    if ! stage_runtime_payload; then
        ERROR "更新载荷准备失败，当前程序未替换"
        return 1
    fi

    # 标记必须先于依赖同步和目录切换写入，进程在任一阶段中断后才能恢复旧代际。
    if ! set_update_pending prepared; then
        ERROR "无法记录更新事务，当前程序未替换"
        return 1
    fi

    if [ "${dependencies_changed}" = "true" ]; then
        if ! set_update_pending dependencies; then
            ERROR "无法记录依赖更新事务，当前程序未替换"
            return 1
        fi
        if ! sync_project_dependencies; then
            ERROR "依赖同步失败，正在恢复更新前的运行环境"
            rollback_update_transaction || true
            return 1
        fi
    fi

    if ! swap_staged_payload; then
        ERROR "程序文件切换失败，正在恢复更新前的运行环境"
        rollback_update_transaction || true
        return 1
    fi
    if ! set_update_pending committed; then
        ERROR "无法确认更新事务，正在恢复更新前的运行环境"
        rollback_update_transaction || true
        return 1
    fi

    if ! finalize_update_transaction; then
        WARN "更新完成，但旧程序备份清理失败，保留事务标记以便下次启动重试"
    fi
    rm -rf "${TMP_PATH}"
    MOVIEPILOT_UPDATE_RESULT="updated"
    INFO "程序更新成功，前端版本：${frontend_version}，后端版本：${1}"
    return 0
}

function probe_package_index() {
    local probe_env=(
        "UV_NO_CACHE=1"
        "UV_HTTP_TIMEOUT=5"
        "UV_HTTP_RETRIES=0"
    )
    local package_index="${1:-}"
    local use_proxy="${2:-false}"
    local probe_dir
    local -a probe_args=(pip install)

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
        probe_args+=(--default-index "${package_index}")
    fi
    probe_args+=(pip-hello-world)

    (
        trap 'rm -rf "${probe_dir}"' EXIT
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM
        timeout --kill-after=2s 10s env "${probe_env[@]}" \
            "${UV_BIN}" "${probe_args[@]}" > /dev/null 2>&1
    )
}

function test_connectivity_package() {
    case "$1" in
    0)
        if [[ -n "${PIP_PROXY}" ]]; then
            if [[ -n "${PROXY_HOST}" ]]; then
                probe_package_index "${PIP_PROXY}" true
            else
                probe_package_index "${PIP_PROXY}" false
            fi
            if [[ $? -eq 0 ]]; then
                UV_OPTIONS=(--default-index "${PIP_PROXY}")
                PACKAGE_LOG="镜像代理模式"
                set_package_proxy_env
                return 0
            fi
        fi
        return 1
        ;;
    1)
        if [[ -n "${PROXY_HOST}" ]]; then
            if probe_package_index "" true; then
                UV_OPTIONS=()
                PACKAGE_LOG="全局代理模式"
                set_package_proxy_env
                return 0
            fi
        fi
        return 1
        ;;
    2)
        PACKAGE_ENV=()
        UV_OPTIONS=()
        PACKAGE_LOG="不使用代理"
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

function configure_package_route() {
    local retries=0
    while true; do
        if test_connectivity_package "${retries}"; then
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
