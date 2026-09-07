#!/bin/bash
# shellcheck shell=bash
# shellcheck disable=SC2016
# shellcheck disable=SC2155

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

function normalize_env_value() {
    printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

function is_truthy_value() {
    local value
    value="$(normalize_env_value "${1:-}")"
    [ "${value}" = "true" ] || [ "${value}" = "1" ] || [ "${value}" = "yes" ]
}

# 设置虚拟环境路径（兼容群晖等系统必须这样配置）
VENV_PATH="${VENV_PATH:-/opt/venv}"
export VENV_PATH
export PATH="${VENV_PATH}/bin:$PATH"
UV_BIN="${UV_BIN:-/usr/local/bin/uv}"

# 校正设置目录
CONFIG_DIR="${CONFIG_DIR:-/config}"
export CONFIG_DIR
MP_CONTROL_DIR="${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}"
export MP_CONTROL_DIR

function apply_package_cache_env() {
    PACKAGE_CACHE_ROOT="${PACKAGE_CACHE_ROOT:-${CONFIG_DIR}/.cache}"
    export PACKAGE_CACHE_ROOT
    export UV_CACHE_DIR="${UV_CACHE_DIR:-${PACKAGE_CACHE_ROOT}/uv}"
    mkdir -p "${UV_CACHE_DIR}"
}

# 环境变量补全
# 优先级: 系统环境变量 -> .env 文件 (即使为空字符串) -> 预设默认值
# 精准适配 Python 端 set_key (quote_mode="always", 单引号包裹, \' 转义)
function load_config_from_app_env() {
    # 保留未配置的新 Dev 开关为空，交由更新器兼容旧模式、Python 持久化迁移。

    local env_file="${CONFIG_DIR}/app.env"

    # 定义 ["变量名"]="预设默认值"
    # 禁止填入 CONFIG_DIR 变量，ACME_ENV_ 开头的变量不设默认值，仅透传 app.env 中已有配置。
    declare -A vars_and_default_values=(
        # update.sh
        ["PIP_PROXY"]=""
        ["PACKAGE_CACHE_ROOT"]=""
        ["GITHUB_PROXY"]=""
        ["PROXY_HOST"]=""
        ["GITHUB_TOKEN"]=""
        ["MOVIEPILOT_AUTO_UPDATE"]="false"
        ["MOVIEPILOT_UPDATE_DEV"]=""
        ["MOVIEPILOT_FORCE_CHOWN"]="false"
        ["MOVIEPILOT_SAFE_MODE"]="false"
        ["BROWSER_EMULATION"]="cloakbrowser"

        # cert
        ["ENABLE_SSL"]="false"
        ["AUTO_ISSUE_CERT"]="false"
        ["SSL_DOMAIN"]=""
        ["SSL_EMAIL"]=""
        ["DNS_PROVIDER"]=""
        ["SSL_NGINX_PORT"]="443"
        ["NGINX_PORT"]="3000"
        ["PORT"]="3001"
        ["NGINX_CLIENT_MAX_BODY_SIZE"]="50m"
    )

    INFO "开始加载配置 (配置文件: ${env_file})..."

    shopt -s extglob

    declare -A values_from_env_file
    if [ -f "${env_file}" ]; then
        INFO "检测到 ${env_file} 文件，尝试解析..."
        while IFS= read -r line || [ -n "$line" ]; do
            if [[ "$line" =~ ^[[:space:]]*# || -z "$line" ]]; then
                continue
            fi

            local key_in_file value_raw_in_file
            if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*) ]]; then
                key_in_file="${BASH_REMATCH[1]}"
                value_raw_in_file="${BASH_REMATCH[2]}"

                if [[ -n "${vars_and_default_values[$key_in_file]+_}" || "${key_in_file}" == ACME_ENV_* ]]; then
                    local temp_val_after_initial_trim
                    temp_val_after_initial_trim="${value_raw_in_file#"${value_raw_in_file%%[![:space:]]*}"}"
                    temp_val_after_initial_trim="${temp_val_after_initial_trim%"${temp_val_after_initial_trim##*[![:space:]]}"}"

                    local val_before_quote_check="${temp_val_after_initial_trim}"
                    if [[ ! ("${temp_val_after_initial_trim:0:1}" == "'" && "${temp_val_after_initial_trim: -1}" == "'") ]]; then
                        if [[ "${temp_val_after_initial_trim}" =~ ^(.*)[[:space:]]+# ]]; then
                            val_before_quote_check="${BASH_REMATCH[1]}"
                            val_before_quote_check="${val_before_quote_check%%+([[:space:]])}"
                        elif [[ "${temp_val_after_initial_trim:0:1}" == "#" ]]; then
                            val_before_quote_check=""
                        fi
                    fi

                    local parsed_value_from_file
                    if [[ "${val_before_quote_check:0:1}" == "'" && "${val_before_quote_check: -1}" == "'" && ${#val_before_quote_check} -ge 2 ]]; then
                        parsed_value_from_file="${val_before_quote_check:1:${#val_before_quote_check}-2}"
                        parsed_value_from_file="${parsed_value_from_file//\\\'/__MP_PARSER_SQUOTE__}"
                        parsed_value_from_file="${parsed_value_from_file//__MP_PARSER_SQUOTE__/\'}"
                    elif [ -z "${val_before_quote_check}" ]; then
                        parsed_value_from_file=""
                    else
                        WARN "位于 ${env_file} 中的键 ${key_in_file} 对应值 ${val_before_quote_check} 未按规范使用单引号包裹，将采用字面量解析。"
                        parsed_value_from_file="${val_before_quote_check}"
                    fi
                    values_from_env_file["${key_in_file}"]="${parsed_value_from_file}"
                fi
            else
                WARN "跳过 ${env_file} 中格式不正确的行: $line"
            fi
        done < <(sed -e '1s/^\xEF\xBB\xBF//' -e 's/\r$//g' "${env_file}")
        INFO "${env_file} 解析完毕。"
     else
        INFO "${env_file} 文件不存在，跳过文件加载。"
     fi

    for var_name in "${!vars_and_default_values[@]}"; do
        local fallback_value="${vars_and_default_values[$var_name]}"
        local final_value
        local value_source="未设置"

        # 检查变量是否在环境中已设置（可能为空）
        if eval "[ -n \"\${${var_name}+x}\" ]"; then
            # 获取其值
            final_value="$(eval echo \"\$"${var_name}"\")"
            value_source="系统环境变量"
        elif [[ -n "${values_from_env_file["${var_name}"]+_}" ]]; then
            final_value="${values_from_env_file["${var_name}"]}"
            value_source=".env 文件"
        else
            final_value="${fallback_value}"
            value_source="内置默认值"
        fi

        if ! declare -g "${var_name}=${final_value}"; then
            ERROR "设置变量 ${var_name}, 值: '${final_value}'失败 (来源: ${value_source}) "
        fi
    done

    for var_name in "${!values_from_env_file[@]}"; do
        if [[ "${var_name}" != ACME_ENV_* ]]; then
            continue
        fi
        if eval "[ -n \"\${${var_name}+x}\" ]"; then
            continue
        fi
        if ! declare -g "${var_name}=${values_from_env_file["${var_name}"]}"; then
            ERROR "设置变量 ${var_name} 失败 (来源: .env 文件) "
        fi
    done

    shopt -u extglob
    INFO "配置加载流程执行完毕。"
}

# 生成 nginx 配置，仅为 envsubst 单次调用传入模板变量。
function render_nginx_config() {
    local https_server_conf
    if [ "${ENABLE_SSL}" = "true" ]; then
        https_server_conf=$(cat <<EOF
    server {
        include /etc/nginx/mime.types;
        default_type application/octet-stream;

        listen ${SSL_NGINX_PORT:-443} ssl;
        listen [::]:${SSL_NGINX_PORT:-443} ssl;
        server_name ${SSL_DOMAIN:-moviepilot};

        # SSL证书路径
        ssl_certificate ${CONFIG_DIR}/certs/latest/fullchain.pem;
        ssl_certificate_key ${CONFIG_DIR}/certs/latest/privkey.pem;

        # SSL安全配置
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384';
        ssl_prefer_server_ciphers on;
        ssl_session_cache shared:SSL:10m;
        ssl_session_timeout 10m;

        # 公共配置
        include common.conf;
    }
EOF
)
        else
            https_server_conf="# HTTPS未启用"
        fi

    NGINX_PORT="${NGINX_PORT}" \
        PORT="${PORT}" \
        NGINX_CLIENT_MAX_BODY_SIZE="${NGINX_CLIENT_MAX_BODY_SIZE}" \
        HTTPS_SERVER_CONF="${https_server_conf}" \
        envsubst '${NGINX_PORT}${PORT}${NGINX_CLIENT_MAX_BODY_SIZE}${HTTPS_SERVER_CONF}' < /etc/nginx/nginx.template.conf > /etc/nginx/nginx.conf
}

# 启动前先检查后端核心依赖是否仍然可导入。
# 插件依赖和主程序共用同一套 venv 时，历史安装记录可能已经污染环境，
# 这里优先在真正拉起后端前做一次自愈，避免容器反复起不来。
function ensure_backend_runtime_dependencies() {
    local probe_module="app.doctor.dependencies"

    INFO "→ 启动前检查后端核心依赖..."
    if "${VENV_PATH}/bin/python3" -m "${probe_module}" >/dev/null 2>&1; then
        INFO "→ 后端核心依赖检查通过。"
        return 0
    fi

    WARN "→ 检测到后端核心依赖异常，开始尝试恢复主程序依赖..."
    if ! configure_package_route; then
        ERROR "→ 无法选择可用的主程序依赖源，后端无法启动。"
        exit 1
    fi
    PACKAGE_ROUTE_READY="true"
    INFO "依赖源：${PACKAGE_LOG}"
    if ! sync_project_dependencies_for "/app" > /dev/stdout 2> /dev/stderr; then
        ERROR "→ 自动恢复主程序依赖失败，后端无法启动。"
        exit 1
    fi

    if ! "${VENV_PATH}/bin/python3" -m "${probe_module}" >/dev/null 2>&1; then
        ERROR "→ 主程序依赖恢复后仍然异常，后端无法启动。"
        exit 1
    fi

    INFO "→ 已自动恢复主程序依赖，继续启动后端。"
}

function path_owner_id() {
    local target="${1:-}"
    [ -n "${target}" ] || return 0
    stat -c '%u:%g' "${target}" 2>/dev/null || stat -f '%u:%g' "${target}" 2>/dev/null || true
}

function force_chown_image_paths_if_requested() {
    if ! is_truthy_value "${MOVIEPILOT_FORCE_CHOWN:-false}"; then
        return 0
    fi

    WARN "→ MOVIEPILOT_FORCE_CHOWN 已启用，将递归修复 /app、/public 权限，可能显著增加启动耗时。"

    local path
    for path in "$@"; do
        [ -e "${path}" ] || continue
        if [ -f "${path}/docker/launcher.sh" ]; then
            # 控制脚本会在下一次启动时以 root 执行，源码目录必须保持不可由运行用户改写。
            chown root:root "${path}" "${path}/docker"
            chmod go-w "${path}" "${path}/docker"
            while IFS= read -r -d '' app_child; do
                chown -R moviepilot:moviepilot "${app_child}"
            done < <(find "${path}" -mindepth 1 -maxdepth 1 ! -name docker -print0)
            continue
        fi
        chown -R moviepilot:moviepilot "${path}"
    done
}

function control_bundle_reexec_decision() {
    local update_result="${1:-noop}"
    local current_generation="${2:-}"
    local next_generation="${3:-}"
    local already_reexecuted="${4:-0}"

    [ "${update_result}" = "updated" ] || return 1
    [ "${next_generation}" != "${current_generation}" ] || return 1
    [ "${already_reexecuted}" != "1" ] || return 2
    return 0
}

function source_control_generation() {
    /entrypoint.sh --source-generation 2>/dev/null
}

function maybe_reexec_control_bundle() {
    [ "${MOVIEPILOT_UPDATE_RESULT:-noop}" = "updated" ] || return 0

    local next_control_generation
    if ! next_control_generation="$(source_control_generation)"; then
        WARN "→ 更新后的容器控制脚本不可用，本次继续使用当前控制脚本快照启动。"
        return 0
    fi

    if control_bundle_reexec_decision \
        "${MOVIEPILOT_UPDATE_RESULT}" \
        "${MP_CONTROL_GENERATION:-}" \
        "${next_control_generation}" \
        "${MOVIEPILOT_BOOTSTRAP_REEXECUTED:-0}"; then
        INFO "→ 检测到容器控制脚本更新，使用新版本继续本次启动。"
        exec /entrypoint.sh --post-update-reexec
    elif [ "$?" -eq 2 ]; then
        ERROR "→ 容器控制脚本在单次启动中重复变化，已终止以避免重启循环。"
        exit 1
    fi
}

function run_pending_dev_update_after_supervisor_shutdown() {
    # 消费由受管重启请求留下的一次性 Dev 更新标记。
    [ -f "${ONE_SHOT_DEV_UPDATE_FLAG}" ] || return 1
    if ! rm -f "${ONE_SHOT_DEV_UPDATE_FLAG}"; then
        ERROR "→ 无法消费一次性 Dev 更新标记，停止启动。"
        return 1
    fi

    local update_exit_code=0
    MOVIEPILOT_UPDATE_DEV="true"
    INFO "检测到受管重启的 Dev 更新请求"
    run_moviepilot_update || update_exit_code=$?
    MOVIEPILOT_UPDATE_DEV="${MOVIEPILOT_UPDATE_DEV_ORIGINAL}"

    [ "${update_exit_code}" -eq 0 ] \
        && [ "${MOVIEPILOT_UPDATE_RESULT:-noop}" = "updated" ]
}

function apply_pending_release_update_at_startup() {
    # worker 尚未启动就发生容器重启时，由 root 入口兜底消费安装清单。
    local install_manifest="${CONFIG_DIR}/temp/moviepilot-update/install.json"
    [ -f "${install_manifest}" ] || return 1
    INFO "检测到未完成的 Release 安装请求，启动前由 root 安装器恢复"
    if ! "${VENV_PATH}/bin/python3" -m app.cli apply-prepared-update; then
        WARN "→ 启动前 Release 更新恢复失败，继续使用当前程序启动。"
        return 1
    fi
    return 0
}

function correct_home_permissions() {
    local child

    [ -e "${HOME}" ] || return 0

    chown moviepilot:moviepilot "${HOME}"
    [ -e "${HOME}/.cloakbrowser" ] && chown -h moviepilot:moviepilot "${HOME}/.cloakbrowser"

    if is_truthy_value "${MOVIEPILOT_FORCE_CHOWN:-false}"; then
        [ -e "${HOME}/.cloakbrowser" ] && chown -R moviepilot:moviepilot "${HOME}/.cloakbrowser"
    elif [ -e "${HOME}/.cloakbrowser" ]; then
        INFO "→ 默认跳过 ${HOME}/.cloakbrowser 递归权限校正，如遇浏览器缓存权限错误可设置 MOVIEPILOT_FORCE_CHOWN=true 后重启一次。"
    fi

    while IFS= read -r -d '' child; do
        [ "${child}" = "${HOME}/.cloakbrowser" ] && continue
        chown_path_excluding_browser_cache "${child}"
    done < <(find "${HOME}" -mindepth 1 -maxdepth 1 -print0)
}

function correct_config_permissions() {
    local child

    [ -e "${CONFIG_DIR}" ] || return 0

    chown moviepilot:moviepilot "${CONFIG_DIR}"
    while IFS= read -r -d '' child; do
        [ "${child}" = "${CONFIG_DIR}/.browser" ] && continue
        chown_path_excluding_browser_cache "${child}"
    done < <(find "${CONFIG_DIR}" -mindepth 1 -maxdepth 1 -print0)
}

function correct_package_cache_permissions() {
    local cache_dir="${UV_CACHE_DIR:-}"
    [ -n "${cache_dir}" ] || return 0
    if [[ "${cache_dir}" != /* ]]; then
        ERROR "→ UV_CACHE_DIR 必须是绝对目录：${cache_dir}"
        return 1
    fi

    local resolved_cache
    local resolved_config
    resolved_cache="$(python3 -c 'import os, sys; print(os.path.normpath(sys.argv[1]))' "${cache_dir}")"
    resolved_config="$(python3 -c 'import os, sys; print(os.path.normpath(sys.argv[1]))' "${CONFIG_DIR}")"
    case "${resolved_cache}/" in
        "${resolved_config}/"*) return 0 ;;
    esac
    case "${resolved_cache}" in
        /|/app|/public|/opt|/usr|/etc|/var|/home|/root|"${VENV_PATH}")
            ERROR "→ UV_CACHE_DIR 不能使用受管根目录：${resolved_cache}"
            return 1
            ;;
    esac

    if ! mkdir -p -- "${resolved_cache}" \
        || ! chown -R moviepilot:moviepilot "${resolved_cache}"; then
        ERROR "→ uv 缓存目录权限修复失败：${resolved_cache}"
        return 1
    fi
    if ! gosu moviepilot:moviepilot sh -c \
        'probe="$1/.moviepilot-write-test.$$"; : > "${probe}" && rm -f "${probe}"' \
        sh "${resolved_cache}"; then
        ERROR "→ uv 缓存目录不可写：${resolved_cache}"
        return 1
    fi
}

function chown_plugin_runtime_path() {
    local plugin_path="${1:-}"
    [ -n "${plugin_path}" ] || return 0
    [ -e "${plugin_path}" ] || return 0
    local current_owner
    current_owner="$(path_owner_id "${plugin_path}")"
    [ "${current_owner}" = "${PUID}:${PGID}" ] && return 0
    chown -h moviepilot:moviepilot "${plugin_path}"
}

function correct_site_resource_permissions() {
    local resource_dir="${IMAGE_RESOURCE_DIR:-/app/app/application/site}"
    [ -e "${resource_dir}" ] || return 0

    INFO "→ 正在修复资源包目录权限：${resource_dir}"
    chown -R moviepilot:moviepilot "${resource_dir}"
}

function correct_file_permissions() {
    local chown_start
    local chown_end
    chown_start=$(date +%s)

    INFO "→ 正在校正文件权限..."
    force_chown_image_paths_if_requested /app /public
    correct_site_resource_permissions
    chown_plugin_runtime_path /app/app/plugins
    correct_home_permissions
    correct_config_permissions
    if ! correct_package_cache_permissions; then
        return 1
    fi
    chown -R moviepilot:moviepilot \
        /var/lib/nginx \
        /var/log/nginx
    chown moviepilot:moviepilot /etc/hosts /tmp

    chown_end=$(date +%s)
    INFO "→ 文件权限校正完成，耗时 $(( chown_end - chown_start )) 秒。"
}

# 使用env配置
load_config_from_app_env
apply_package_cache_env

# Dev 手动更新仍沿用一次性标记；Release 安装由 root 更新 worker 在重启前完成。
ONE_SHOT_DEV_UPDATE_FLAG="${CONFIG_DIR}/temp/moviepilot.pending_dev_update"
SUPERVISOR_RESTART_REQUEST_FILE="${CONFIG_DIR}/temp/moviepilot.pending_supervisor_restart"
ONE_SHOT_DEV_UPDATE="false"
MOVIEPILOT_UPDATE_DEV_ORIGINAL="${MOVIEPILOT_UPDATE_DEV}"
if [ -f "${ONE_SHOT_DEV_UPDATE_FLAG}" ]; then
    rm -f "${ONE_SHOT_DEV_UPDATE_FLAG}"
    MOVIEPILOT_UPDATE_DEV="true"
    ONE_SHOT_DEV_UPDATE="true"
    INFO "检测到一次性 Dev 更新标记，本次启动将更新开发分支"
fi

# 使用env配置渲染 nginx 配置
render_nginx_config

# 自动更新，控制脚本由 launcher 固化到同一代运行目录，源码替换不会改变本轮执行内容。
cd /
source "${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}/update.sh"
if [ "${MOVIEPILOT_BOOTSTRAP_UPDATE_DONE:-0}" != "1" ]; then
    if ! recover_pending_update; then
        ERROR "→ 上一次容器更新未能恢复，停止启动。"
        exit 1
    fi
    if [ "${UPDATE_RECOVERY_COMPLETED:-false}" = "true" ]; then
        INFO "→ 已恢复到更新前版本，本次启动跳过自动更新。"
    else
        run_moviepilot_update
    fi
    export MOVIEPILOT_BOOTSTRAP_UPDATE_DONE=1
else
    MOVIEPILOT_UPDATE_RESULT="noop"
fi
if [ "${ONE_SHOT_DEV_UPDATE}" = "true" ]; then
    MOVIEPILOT_UPDATE_DEV="${MOVIEPILOT_UPDATE_DEV_ORIGINAL}"
fi
if [ "${UPDATE_RECOVERY_REQUIRED:-false}" = "true" ]; then
    ERROR "→ 容器更新回滚未完成，停止启动。"
    exit 1
fi

maybe_reexec_control_bundle
cd /app || exit

if [ "${MOVIEPILOT_BOOTSTRAP_UPDATE_DONE:-0}" != "1" ] \
    && [ -f "${CONFIG_DIR}/temp/moviepilot-update/install.json" ]; then
    if apply_pending_release_update_at_startup; then
        INFO "→ 未完成的 Release 更新已安装，重新执行入口加载新代码。"
        exec /entrypoint.sh --post-update-reexec
    fi
fi

source "${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}/browser.sh"

# 更改 moviepilot userid 和 groupid
groupmod -o -g "${PGID}" moviepilot
usermod -o -u "${PUID}" moviepilot

# 启动前优先确认主运行环境仍然健康，避免插件依赖污染导致服务直接起不来。
ensure_backend_runtime_dependencies || exit 1

# 依赖阶段恢复会保留当前程序，待自愈成功后再清理旧代际备份和事务标记。
if [ "${UPDATE_RECOVERY_BLOCKED:-false}" = "true" ]; then
    if finalize_update_transaction; then
        INFO "→ 当前程序依赖已恢复，已清理保留当前程序的更新事务"
    else
        WARN "→ 当前程序依赖已恢复，但旧代际备份清理失败，将保留事务标记重试"
    fi
fi

# 缓存路径解析必须晚于依赖自愈，确保有效性探针使用当前运行版本。
if ! resolve_browser_cache_dir; then
    exit 1
fi

# 权限校正需要避开选中的浏览器缓存子树，避免启动时递归扫描内核文件。
correct_file_permissions

if ! prepare_browser_cache_dir; then
    exit 1
fi
ensure_browser_kernel

# 证书管理
source "${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}/cert.sh"

# supervisord 常驻前台并统一托管 Nginx 与后端；带更新标记的 shutdown 会回到本入口消费更新包。
install -d -m 0755 /run/moviepilot
# Supervisor 的控制面只在容器内使用；未显式传入时生成本次容器启动专用的随机凭据，避免固定密码进入镜像。
if [ -z "${MOVIEPILOT_SUPERVISOR_PASSWORD:-}" ]; then
    MOVIEPILOT_SUPERVISOR_PASSWORD="$(openssl rand -hex 32)" || {
        ERROR "→ 无法生成 supervisor 控制面认证凭据，停止启动。"
        exit 1
    }
fi
if [ -z "${MOVIEPILOT_SUPERVISOR_PASSWORD}" ]; then
    ERROR "→ supervisor 控制面认证凭据为空，停止启动。"
    exit 1
fi
export MOVIEPILOT_SUPERVISOR_PASSWORD
SUPERVISOR_SIGNAL_RECEIVED="false"
SUPERVISOR_PID=""
function forward_supervisor_signal() {
    SUPERVISOR_SIGNAL_RECEIVED="true"
    if [ -n "${SUPERVISOR_PID}" ]; then
        kill -TERM "${SUPERVISOR_PID}" 2>/dev/null || true
    fi
}
trap 'forward_supervisor_signal' SIGINT SIGTERM
while true; do
    INFO "→ 启动容器进程 supervisor..."
    /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf &
    SUPERVISOR_PID=$!
    wait "${SUPERVISOR_PID}"
    supervisor_exit_code=$?
    SUPERVISOR_PID=""

    if [ "${SUPERVISOR_SIGNAL_RECEIVED}" = "true" ] || [ "${supervisor_exit_code}" -ne 0 ]; then
        exit "${supervisor_exit_code}"
    fi

    if [ -f "${SUPERVISOR_RESTART_REQUEST_FILE}" ]; then
        if ! rm -f "${SUPERVISOR_RESTART_REQUEST_FILE}"; then
            ERROR "→ 无法消费更新后的重启请求，停止启动。"
            exit 1
        fi
        INFO "→ 更新代码已落盘，重新执行容器入口以加载新版本。"
        exec /entrypoint.sh --post-update-reexec
    fi

    if [ -f "${ONE_SHOT_DEV_UPDATE_FLAG}" ]; then
        if run_pending_dev_update_after_supervisor_shutdown; then
            INFO "→ 更新包已安装，重新执行容器入口以加载新版本。"
            exec /entrypoint.sh --post-update-reexec
        fi
        if [ -f "${ONE_SHOT_DEV_UPDATE_FLAG}" ]; then
            ERROR "→ 更新请求未能完成且标记仍存在，停止启动。"
            exit 1
        fi
        WARN "→ Dev 更新失败，继续启动当前版本。"
        continue
    fi

    exit 0
done
