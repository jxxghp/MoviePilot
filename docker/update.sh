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
UPDATE_PAYLOAD_ASSET="source-update-payload.json"

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
TARGET_UPDATE_CHANNEL=""
TARGET_RELEASE_GENERATION=""
TARGET_PAYLOAD_SHA256=""
TARGET_PAYLOAD_UNCHANGED="false"
TARGET_BACKEND_REVISION=""
TARGET_FRONTEND_VERSION=""
TARGET_FRONTEND_SHA256=""
TARGET_RESOURCES_REVISION=""
TARGET_RESOURCE_INDEX_SHA256=""
TARGET_RESOURCE_SITES_SHA256=""
TARGET_PAYLOAD_FILE=""

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

function download_file() {
    local retries=0
    local max_retries=3
    local url="$1"
    local destination="$2"
    local expected_sha256="${3:-}"
    local temporary="${destination}.part.$$"

    INFO "→ 正在下载 ${url}..."
    while [ $retries -lt $max_retries ]; do
        rm -f "${temporary}"
        if curl ${CURL_OPTIONS} --fail "${url}" ${CURL_HEADERS} -o "${temporary}" \
            && [ -s "${temporary}" ]; then
            if [ -n "${expected_sha256}" ] \
                && ! printf '%s  %s\n' "${expected_sha256}" "${temporary}" | sha256sum -c - > /dev/null; then
                ERROR "${url} 完整性校验失败"
                rm -f "${temporary}"
                return 1
            fi
            mv -f "${temporary}" "${destination}"
            return 0
        fi
        WARN "下载 ${url} 失败，正在进行第 $((retries + 1)) 次重试..."
        retries=$((retries + 1))
    done

    rm -f "${temporary}"
    ERROR "下载 ${url} 失败，已达到最大重试次数！"
    return 1
}

# 下载及解压
function download_and_unzip() {
    local url="$1"
    local target_dir="$2"
    local expected_sha256="${3:-}"
    local archive="${TMP_PATH}/.${target_dir}.zip"
    local extracted_dir

    download_file "${url}" "${archive}" "${expected_sha256}" || return 1
    if ! busybox unzip -t "${archive}" > /dev/null \
        || ! busybox unzip -q "${archive}" -d "${TMP_PATH}"; then
        ERROR "${url} 不是完整的 ZIP 制品"
        return 1
    fi
    if [ ! -d "${TMP_PATH}/${target_dir}" ]; then
        extracted_dir=$(find "${TMP_PATH}" -mindepth 1 -maxdepth 1 -type d \
            -name 'MoviePilot-*' -print -quit)
        if [ -n "${extracted_dir}" ]; then
            mv "${extracted_dir}" "${TMP_PATH}/${target_dir}" || return 1
        fi
    fi
    [ -d "${TMP_PATH}/${target_dir}" ]
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

function resource_sites_file() {
    local python_version
    local arch
    local arch_suffix

    python_version="$(python3 -c 'import sys; print(f"cpython-{sys.version_info.major}{sys.version_info.minor}")')" \
        || return 1
    arch="$(uname -m)"
    case "${arch}" in
        aarch64|arm64) arch_suffix="aarch64-linux-gnu" ;;
        x86_64|amd64) arch_suffix="x86_64-linux-gnu" ;;
        *)
            ERROR "不支持的更新资源架构：${arch}"
            return 1
            ;;
    esac
    printf 'sites.%s-%s.so\n' "${python_version}" "${arch_suffix}"
}

function payload_identity_file() {
    printf '%s/.moviepilot-payload.json\n' "${APP_DIR}"
}

function installed_payload_value() {
    local field="$1"
    local identity_file
    identity_file="$(payload_identity_file)"

    if [ -f "${identity_file}" ] \
        && jq -e '.schema_version == 2' "${identity_file}" > /dev/null 2>&1; then
        jq -er --arg field "${field}" '.[$field] | strings | select(length > 0)' \
            "${identity_file}" 2>/dev/null
        return $?
    fi

    case "${field}" in
        channel) printf '%s\n' "${MOVIEPILOT_IMAGE_UPDATE_CHANNEL:-}" ;;
        release_generation) printf '%s\n' "${MOVIEPILOT_IMAGE_RELEASE_GENERATION:-}" ;;
        payload_sha256) printf '%s\n' "${MOVIEPILOT_IMAGE_SOURCE_UPDATE_PAYLOAD_SHA256:-}" ;;
        backend_revision) printf '%s\n' "${MOVIEPILOT_IMAGE_BACKEND_REVISION:-}" ;;
        frontend_version) printf '%s\n' "${MOVIEPILOT_IMAGE_FRONTEND_VERSION:-}" ;;
        frontend_sha256) printf '%s\n' "${MOVIEPILOT_IMAGE_FRONTEND_SHA256:-}" ;;
        resources_revision) printf '%s\n' "${MOVIEPILOT_IMAGE_RESOURCES_REVISION:-}" ;;
        *) return 1 ;;
    esac
}

function validate_target_payload() {
    local sites_file="$1"

    [[ "${TARGET_UPDATE_CHANNEL}" =~ ^(release|dev)$ ]] \
        && { [ "${TARGET_UPDATE_CHANNEL}" = "dev" ] \
            || [[ "${TARGET_RELEASE_GENERATION}" =~ ^[1-9][0-9]*\.[1-9][0-9]*$ ]]; } \
        && { [ "${TARGET_UPDATE_CHANNEL}" = "dev" ] \
            || [[ "${TARGET_PAYLOAD_SHA256}" =~ ^[0-9a-f]{64}$ ]]; } \
        && [[ "${TARGET_BACKEND_REVISION}" =~ ^[0-9a-f]{40}$ ]] \
        && [[ "${TARGET_FRONTEND_VERSION}" =~ ^v3\.[0-9A-Za-z._-]+$ ]] \
        && [[ "${TARGET_FRONTEND_SHA256}" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "${TARGET_RESOURCES_REVISION}" =~ ^[0-9a-f]{40}$ ]] \
        && { [ -z "${TARGET_RESOURCE_INDEX_SHA256}" ] \
            || [[ "${TARGET_RESOURCE_INDEX_SHA256}" =~ ^[0-9a-f]{64}$ ]]; } \
        && { [ -z "${TARGET_RESOURCE_SITES_SHA256}" ] \
            || [[ "${TARGET_RESOURCE_SITES_SHA256}" =~ ^[0-9a-f]{64}$ ]]; } \
        && [[ "${sites_file}" =~ ^sites\.cpython-[0-9]+-(x86_64|aarch64)-linux-gnu\.so$ ]]
}

function load_release_payload() {
    local release_tag="$1"
    local sites_file
    local payload_url
    local release_metadata="${TMP_PATH}/latest-v3-release.json"
    local payload_digest

    TARGET_PAYLOAD_SHA256=""
    TARGET_PAYLOAD_UNCHANGED="false"
    if [ ! -s "${release_metadata}" ]; then
        curl ${CURL_OPTIONS} --compressed --fail --connect-timeout 5 --max-time 15 \
            "https://api.github.com/repos/jxxghp/MoviePilot/releases/tags/${release_tag}" \
            ${CURL_HEADERS} > "${release_metadata}" || return 1
    fi
    payload_digest=$(jq -er --arg asset "${UPDATE_PAYLOAD_ASSET}" \
        '.assets[] | select(.name == $asset) | .digest' "${release_metadata}") || return 1
    TARGET_PAYLOAD_SHA256="${payload_digest#sha256:}"
    [[ "${TARGET_PAYLOAD_SHA256}" =~ ^[0-9a-f]{64}$ ]] || return 1
    if [ "$(installed_payload_value payload_sha256 2>/dev/null)" = "${TARGET_PAYLOAD_SHA256}" ]; then
        TARGET_PAYLOAD_UNCHANGED="true"
        return 0
    fi

    sites_file="$(resource_sites_file)" || return 1
    TARGET_PAYLOAD_FILE="${TMP_PATH}/${UPDATE_PAYLOAD_ASSET}"
    payload_url="${GITHUB_PROXY}https://github.com/jxxghp/MoviePilot/releases/download/${release_tag}/${UPDATE_PAYLOAD_ASSET}"
    download_file "${payload_url}" "${TARGET_PAYLOAD_FILE}" "${TARGET_PAYLOAD_SHA256}" || return 1
    if ! jq -e '.schema_version == 2 and .channel == "release"' \
        "${TARGET_PAYLOAD_FILE}" > /dev/null; then
        ERROR "发布载荷清单格式无效"
        return 1
    fi
    TARGET_UPDATE_CHANNEL="$(jq -r '.channel' "${TARGET_PAYLOAD_FILE}")"
    TARGET_RELEASE_GENERATION="$(jq -r '.release_generation' "${TARGET_PAYLOAD_FILE}")"
    TARGET_BACKEND_REVISION="$(jq -r '.backend_revision' "${TARGET_PAYLOAD_FILE}")"
    TARGET_FRONTEND_VERSION="$(jq -r '.frontend_version' "${TARGET_PAYLOAD_FILE}")"
    TARGET_FRONTEND_SHA256="$(jq -r '.frontend_sha256' "${TARGET_PAYLOAD_FILE}")"
    TARGET_RESOURCES_REVISION="$(jq -r '.resources_revision' "${TARGET_PAYLOAD_FILE}")"
    TARGET_RESOURCE_INDEX_SHA256="$(jq -r '.resource_sha256["user.sites.v3.bin"] // ""' \
        "${TARGET_PAYLOAD_FILE}")"
    TARGET_RESOURCE_SITES_SHA256="$(jq -r --arg sites_file "${sites_file}" \
        '.resource_sha256[$sites_file] // ""' "${TARGET_PAYLOAD_FILE}")"
    if ! validate_target_payload "${sites_file}" \
        || [ -z "${TARGET_RESOURCE_INDEX_SHA256}" ] \
        || [ -z "${TARGET_RESOURCE_SITES_SHA256}" ]; then
        ERROR "发布载荷清单缺少当前平台的完整身份"
        return 1
    fi
}

function github_commit_revision() {
    local repository="$1"
    local ref="$2"
    local revision

    revision=$(curl ${CURL_OPTIONS} --compressed --fail --connect-timeout 5 --max-time 15 \
        "https://api.github.com/repos/${repository}/commits/${ref}" ${CURL_HEADERS} \
        | jq -er '.sha') || return 1
    [[ "${revision}" =~ ^[0-9a-f]{40}$ ]] || return 1
    printf '%s\n' "${revision}"
}

function fetch_latest_frontend_v3_release() {
    local response
    local release_tag

    response=$(curl ${CURL_OPTIONS} --compressed --fail --connect-timeout 5 --max-time 15 \
        "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases" \
        ${CURL_HEADERS}) || return 1
    release_tag=$(printf '%s\n' "${response}" | jq -r '.[].tag_name' \
        | grep '^v3\.' | sort -V | tail -n 1)
    [[ -n "${release_tag}" ]] || return 1
    printf '%s\n' "${release_tag}"
}

function frontend_release_sha256() {
    local release_tag="$1"
    local digest

    digest=$(curl ${CURL_OPTIONS} --compressed --fail --connect-timeout 5 --max-time 15 \
        "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases/tags/${release_tag}" \
        ${CURL_HEADERS} \
        | jq -er '.assets[] | select(.name == "dist.zip") | .digest') || return 1
    digest="${digest#sha256:}"
    [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "${digest}"
}

function load_dev_payload() {
    local sites_file

    sites_file="$(resource_sites_file)" || return 1
    TARGET_UPDATE_CHANNEL="dev"
    TARGET_RELEASE_GENERATION=""
    TARGET_PAYLOAD_SHA256=""
    TARGET_PAYLOAD_UNCHANGED="false"
    TARGET_BACKEND_REVISION="$(github_commit_revision jxxghp/MoviePilot v3)" || return 1
    TARGET_FRONTEND_VERSION="$(fetch_latest_frontend_v3_release)" || return 1
    TARGET_FRONTEND_SHA256="$(frontend_release_sha256 "${TARGET_FRONTEND_VERSION}")" || return 1
    TARGET_RESOURCES_REVISION="$(github_commit_revision jxxghp/MoviePilot-Resources main)" || return 1
    TARGET_RESOURCE_INDEX_SHA256=""
    TARGET_RESOURCE_SITES_SHA256=""
    validate_target_payload "${sites_file}"
}

function installed_payload_matches_target() {
    [ "$(installed_payload_value channel 2>/dev/null)" = "${TARGET_UPDATE_CHANNEL}" ] \
        && [ "$(installed_payload_value release_generation 2>/dev/null)" = "${TARGET_RELEASE_GENERATION}" ] \
        && [ "$(installed_payload_value payload_sha256 2>/dev/null)" = "${TARGET_PAYLOAD_SHA256}" ] \
        && [ "$(installed_payload_value backend_revision 2>/dev/null)" = "${TARGET_BACKEND_REVISION}" ] \
        && [ "$(installed_payload_value frontend_version 2>/dev/null)" = "${TARGET_FRONTEND_VERSION}" ] \
        && [ "$(installed_payload_value frontend_sha256 2>/dev/null)" = "${TARGET_FRONTEND_SHA256}" ] \
        && [ "$(installed_payload_value resources_revision 2>/dev/null)" = "${TARGET_RESOURCES_REVISION}" ]
}

function target_release_is_older_than_installed() {
    local installed_generation
    local installed_run
    local installed_attempt
    local target_run
    local target_attempt

    [ "${TARGET_UPDATE_CHANNEL}" = "release" ] \
        && [ "$(installed_payload_value channel 2>/dev/null)" = "release" ] || return 1
    installed_generation="$(installed_payload_value release_generation 2>/dev/null)"
    [[ "${installed_generation}" =~ ^[1-9][0-9]*\.[1-9][0-9]*$ ]] \
        && [[ "${TARGET_RELEASE_GENERATION}" =~ ^[1-9][0-9]*\.[1-9][0-9]*$ ]] || return 1

    installed_run="${installed_generation%.*}"
    installed_attempt="${installed_generation##*.}"
    target_run="${TARGET_RELEASE_GENERATION%.*}"
    target_attempt="${TARGET_RELEASE_GENERATION##*.}"
    (( 10#${target_run} < 10#${installed_run} \
        || (10#${target_run} == 10#${installed_run} \
            && 10#${target_attempt} < 10#${installed_attempt}) ))
}

function write_staged_payload_identity() {
    local stage_app="${TMP_PATH}/App"
    local stage_resource_dir="${stage_app}/app/application/site"
    local sites_file
    local resource_index_sha256
    local resource_sites_sha256

    sites_file="$(resource_sites_file)" || return 1
    resource_index_sha256=$(sha256sum "${stage_resource_dir}/user.sites.v3.bin" | awk '{print $1}') \
        || return 1
    resource_sites_sha256=$(sha256sum "${stage_resource_dir}/${sites_file}" | awk '{print $1}') \
        || return 1
    jq -n \
        --arg channel "${TARGET_UPDATE_CHANNEL}" \
        --arg release_generation "${TARGET_RELEASE_GENERATION}" \
        --arg payload_sha256 "${TARGET_PAYLOAD_SHA256}" \
        --arg backend_revision "${TARGET_BACKEND_REVISION}" \
        --arg frontend_version "${TARGET_FRONTEND_VERSION}" \
        --arg frontend_sha256 "${TARGET_FRONTEND_SHA256}" \
        --arg resources_revision "${TARGET_RESOURCES_REVISION}" \
        --arg resource_index_sha256 "${resource_index_sha256}" \
        --arg sites_file "${sites_file}" \
        --arg resource_sites_sha256 "${resource_sites_sha256}" \
        '{
            schema_version: 2,
            channel: $channel,
            release_generation: $release_generation,
            payload_sha256: $payload_sha256,
            backend_revision: $backend_revision,
            frontend_version: $frontend_version,
            frontend_sha256: $frontend_sha256,
            resources_revision: $resources_revision,
            resource_sha256: {
                "user.sites.v3.bin": $resource_index_sha256,
                ($sites_file): $resource_sites_sha256
            }
        }' > "${stage_app}/.moviepilot-payload.json"
}

function stage_runtime_payload() {
    local stage_app="${TMP_PATH}/App"
    local stage_plugin_dir="${stage_app}/app/plugins"
    local stage_resource_dir="${stage_app}/app/application/site"
    local sites_file

    [ -f "${stage_app}/version.py" ] || return 1
    [ -f "${stage_app}/pyproject.toml" ] || return 1
    [ -f "${stage_app}/uv.lock" ] || return 1
    [ -f "${TMP_PATH}/dist/index.html" ] || return 1

    if [ -d "${APP_DIR}/app/plugins" ]; then
        rm -rf "${stage_plugin_dir}" || return 1
        mkdir -p "${stage_plugin_dir}" || return 1
        if ! cp -a "${APP_DIR}/app/plugins/." "${stage_plugin_dir}/"; then
            return 1
        fi
    else
        mkdir -p "${stage_plugin_dir}" || return 1
    fi
    # 保留 app.plugins 兼容入口；V1/V2 插件仍从这里导入 _PluginBase。
    # 删除后 app.plugins 会退化为 namespace package，旧插件会在启动时全部导入失败。
    if [ ! -f "${stage_plugin_dir}/__init__.py" ]; then
        ERROR "插件运行目录缺少 app.plugins 兼容入口"
        return 1
    fi

    mkdir -p "${stage_resource_dir}" || return 1
    sites_file="$(resource_sites_file)" || return 1
    download_file \
        "${GITHUB_PROXY}https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/${TARGET_RESOURCES_REVISION}/resources.v3/user.sites.v3.bin" \
        "${stage_resource_dir}/user.sites.v3.bin" \
        "${TARGET_RESOURCE_INDEX_SHA256}" || return 1
    download_file \
        "${GITHUB_PROXY}https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/${TARGET_RESOURCES_REVISION}/resources.v3/${sites_file}" \
        "${stage_resource_dir}/${sites_file}" \
        "${TARGET_RESOURCE_SITES_SHA256}" || return 1
    write_staged_payload_identity || return 1
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
    local backend_url
    local frontend_version
    local release_tag

    if [[ "${1}" == "heads/v3.zip" ]]; then
        if ! load_dev_payload; then
            ERROR "Dev 更新载荷身份解析失败"
            return 1
        fi
    else
        release_tag="${1#tags/}"
        release_tag="${release_tag%.zip}"
        if ! load_release_payload "${release_tag}"; then
            ERROR "Release 更新载荷身份解析失败"
            return 1
        fi
        if [ "${TARGET_PAYLOAD_UNCHANGED}" = "true" ]; then
            INFO "发布载荷身份未变化，跳过载荷下载"
            return 0
        fi
    fi
    if target_release_is_older_than_installed; then
        INFO "远程发布清单早于当前镜像，跳过旧代际载荷"
        return 0
    fi
    if installed_payload_matches_target; then
        INFO "后端、前端和站点资源身份均未变化，跳过载荷下载"
        return 0
    fi

    # 更新后端程序
    backend_url="${GITHUB_PROXY}https://github.com/jxxghp/MoviePilot/archive/${TARGET_BACKEND_REVISION}.zip"
    if ! download_and_unzip "${backend_url}" "App"; then
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
    
    frontend_version=$(sed -n "s/^FRONTEND_VERSION\s*=\s*'\([^']*\)'/\1/p" "${TMP_PATH}/App/version.py")
    if [ "${TARGET_UPDATE_CHANNEL}" = "release" ] \
        && [ "${frontend_version}" != "${TARGET_FRONTEND_VERSION}" ]; then
        ERROR "后端源码声明的前端版本与发布载荷清单不一致"
        return 1
    fi
    INFO "前端版本号：${TARGET_FRONTEND_VERSION}"
    # 更新前端程序
    if ! download_and_unzip \
        "${GITHUB_PROXY}https://github.com/jxxghp/MoviePilot-Frontend/releases/download/${TARGET_FRONTEND_VERSION}/dist.zip" \
        "dist" \
        "${TARGET_FRONTEND_SHA256}"; then
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
    INFO "程序更新成功，前端版本：${TARGET_FRONTEND_VERSION}，后端 revision：${TARGET_BACKEND_REVISION}"
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
    local release_metadata="${TMP_PATH}/latest-v3-release.json"

    response=$(curl ${CURL_OPTIONS} --compressed --fail --connect-timeout 5 --max-time 15 \
        "https://api.github.com/repos/jxxghp/MoviePilot/releases" \
        ${CURL_HEADERS}) || return 1
    releases=$(printf '%s\n' "${response}" | jq -r '.[].tag_name') || return 1
    latest_release=$(printf '%s\n' "${releases}" | grep "^v3\." | sort -V | tail -n 1)
    [[ -n "${latest_release}" ]] || return 1
    printf '%s\n' "${response}" \
        | jq -e --arg tag "${latest_release}" '.[] | select(.tag_name == $tag)' \
            > "${release_metadata}" || return 1
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
    if [ "$(installed_payload_value channel 2>/dev/null)" = "release" ]; then
        INFO "当前版本号未变化，继续核对发布载荷身份..."
        if install_backend_and_download_resources "tags/$2.zip"; then
            return 0
        fi
        MOVIEPILOT_UPDATE_RESULT="failed"
        return 1
    fi
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
