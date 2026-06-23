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

# 设置虚拟环境路径（兼容群晖等系统必须这样配置）
VENV_PATH="${VENV_PATH:-/opt/venv}"
export PATH="${VENV_PATH}/bin:$PATH"

# 校正设置目录
CONFIG_DIR="${CONFIG_DIR:-/config}"

function apply_package_cache_env() {
    PACKAGE_CACHE_ROOT="${PACKAGE_CACHE_ROOT:-${CONFIG_DIR}/.cache}"
    export PACKAGE_CACHE_ROOT
    export PIP_CACHE_DIR="${PIP_CACHE_DIR:-${PACKAGE_CACHE_ROOT}/pip}"
    export UV_CACHE_DIR="${UV_CACHE_DIR:-${PACKAGE_CACHE_ROOT}/uv}"
    mkdir -p "${PIP_CACHE_DIR}" "${UV_CACHE_DIR}"
}

function apply_package_proxy_env() {
    if [ -n "${PROXY_HOST}" ]; then
        export HTTP_PROXY="${PROXY_HOST}"
        export HTTPS_PROXY="${PROXY_HOST}"
        export http_proxy="${PROXY_HOST}"
        export https_proxy="${PROXY_HOST}"
    fi
}

# 环境变量补全
# 优先级: 系统环境变量 -> .env 文件 (即使为空字符串) -> 预设默认值
# 精准适配 Python 端 set_key (quote_mode="always", 单引号包裹, \' 转义)
function load_config_from_app_env() {

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
        ["MOVIEPILOT_AUTO_UPDATE"]="release"
        ["MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE"]="true"
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

# 优雅退出
function graceful_exit() {
    local exit_code=${1:-0}
    local reason=${2:-python_exit}

    if [ "$reason" = "signal" ]; then
        INFO "→ 收到停止信号，执行精准清理程序..."
    elif [ "$reason" = "intentional_restart" ]; then
        INFO "→ 检测到内置重启流程，执行清理程序..."
    else
        INFO "→ 主进程已退出 (代码: $exit_code)，执行清理程序..."
    fi

    # 第一步：停止前端 Nginx
    # 默认配置启动的 Nginx，默认 PID 在 /var/run/nginx.pid
    INFO "→ [1/3] 正在关闭前端 Nginx..."
    nginx -c /etc/nginx/nginx.conf -s stop 2>/dev/null || true

    # 第二步：等待 Python 退出
    # 由于使用了 tini -g，Python 已经收到了信号，我们只需等待
    if [ -n "$PYTHON_PID" ] && ps -p "$PYTHON_PID" > /dev/null; then
        INFO "→ [2/3] 正在等待 Python (PID: $PYTHON_PID) 完成清理..."
        # 这里的 wait 会阻塞，直到 Python 真正退出
        wait "$PYTHON_PID" 2>/dev/null || true
    fi

    # 第三步：最后关闭 Docker Proxy
    # 必须指定配置文件路径，否则 nginx -s stop 找不到它
    INFO "→ [3/3] 后端已安全退出，正在关闭 Docker Proxy..."
    if [ -S "/var/run/docker.sock" ]; then
        nginx -c /etc/nginx/docker_http_proxy.conf -s stop 2>/dev/null || true
    fi

    # 根据退出码判断最终日志性质
    # 0: 正常退出
    # 130/143: 被系统信号终止（通常也视为预期的清理退出）
    if [ "$exit_code" -eq 0 ] || [ "$exit_code" -eq 130 ] || [ "$exit_code" -eq 143 ] || [ "$reason" = "intentional_restart" ]; then
        INFO "→ 所有服务已按序清理，容器正常退出 (ExitCode: $exit_code)。"
    else
        # 非预期退出码，使用 ERROR 级别并加重提示
        ERROR "→ 清理完成，但主进程检测到异常退出 (ExitCode: $exit_code)！"
    fi
    exit "$exit_code"
}

# 后端异常退出时默认保留容器，避免无法 docker exec 进入容器运行 doctor。
function diagnostic_keepalive() {
    local exit_code=${1:-1}
    local keepalive="${MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE:-true}"
    keepalive="${keepalive,,}"

    if [ "${keepalive}" = "false" ] || [ "${keepalive}" = "0" ] || [ "${keepalive}" = "no" ]; then
        graceful_exit "$exit_code" "python_exit"
    fi

    ERROR "→ 后端主进程异常退出 (ExitCode: ${exit_code})，容器将保持运行以便执行 moviepilot doctor。"
    WARN "→ 可运行：docker exec <container> moviepilot doctor"
    WARN "→ 如需恢复旧行为，可设置 MOVIEPILOT_DOCKER_KEEPALIVE_ON_FAILURE=false。"

    if [ "${START_NOGOSU:-false}" = "true" ]; then
        "${VENV_PATH}/bin/python3" -m app.cli doctor || true
    else
        gosu moviepilot:moviepilot "${VENV_PATH}/bin/python3" -m app.cli doctor || true
    fi

    while true; do
        sleep 3600 &
        wait $! || true
    done
}

# 启动前先检查后端核心依赖是否仍然可导入。
# 插件依赖和主程序共用同一套 venv 时，历史安装记录可能已经污染环境，
# 这里优先在真正拉起后端前做一次自愈，避免容器反复起不来。
function ensure_backend_runtime_dependencies() {
    local probe_code="import alembic, cloakbrowser, fastapi, pydantic, pydantic_core, pydantic_settings, sqlalchemy, starlette, uvicorn; from pydantic import BaseModel, Field"

    INFO "→ 启动前检查后端核心依赖..."
    if "${VENV_PATH}/bin/python3" -c "${probe_code}" >/dev/null 2>&1; then
        INFO "→ 后端核心依赖检查通过。"
        return 0
    fi

    WARN "→ 检测到后端核心依赖异常，开始尝试恢复主程序依赖..."
    apply_package_proxy_env
    local -a pip_cmd=("${VENV_PATH}/bin/pip" "install" "-r" "/app/requirements.txt")
    if [ -n "${PIP_PROXY}" ]; then
        pip_cmd+=("-i" "${PIP_PROXY}")
    fi

    if ! "${pip_cmd[@]}" > /dev/stdout 2> /dev/stderr; then
        ERROR "→ 自动恢复主程序依赖失败，后端无法启动。"
        diagnostic_keepalive 1
    fi

    if ! "${VENV_PATH}/bin/python3" -c "${probe_code}" >/dev/null 2>&1; then
        ERROR "→ 主程序依赖恢复后仍然异常，后端无法启动。"
        diagnostic_keepalive 1
    fi

    INFO "→ 已自动恢复主程序依赖，继续启动后端。"
}

# 使用env配置
load_config_from_app_env
apply_package_cache_env

# 一次性升级标记仅影响本次启动，避免把临时升级模式带入运行中的 Python 进程
ONE_SHOT_UPDATE_FLAG="${CONFIG_DIR}/temp/moviepilot.pending_update"
ONE_SHOT_UPDATE_APPLIED="false"
MOVIEPILOT_AUTO_UPDATE_ORIGINAL="${MOVIEPILOT_AUTO_UPDATE}"
if [ -f "${ONE_SHOT_UPDATE_FLAG}" ]; then
    ONE_SHOT_UPDATE_MODE="$(tr -d '\r\n' < "${ONE_SHOT_UPDATE_FLAG}" | tr '[:upper:]' '[:lower:]')"
    rm -f "${ONE_SHOT_UPDATE_FLAG}"
    if [ "${ONE_SHOT_UPDATE_MODE}" = "true" ]; then
        ONE_SHOT_UPDATE_MODE="release"
    fi
    if [ "${ONE_SHOT_UPDATE_MODE}" = "release" ] || [ "${ONE_SHOT_UPDATE_MODE}" = "dev" ]; then
        INFO "检测到一次性升级标记，本次启动将执行 ${ONE_SHOT_UPDATE_MODE} 升级..."
        MOVIEPILOT_AUTO_UPDATE="${ONE_SHOT_UPDATE_MODE}"
        ONE_SHOT_UPDATE_APPLIED="true"
    elif [ -n "${ONE_SHOT_UPDATE_MODE}" ]; then
        WARN "检测到无效的一次性升级模式：${ONE_SHOT_UPDATE_MODE}，已忽略"
    fi
fi

# 使用env配置渲染 nginx 配置
render_nginx_config

# 自动更新
cd /
source /usr/local/bin/mp_update.sh
if [ "${ONE_SHOT_UPDATE_APPLIED}" = "true" ]; then
    MOVIEPILOT_AUTO_UPDATE="${MOVIEPILOT_AUTO_UPDATE_ORIGINAL}"
fi
cd /app || exit

# 更改 moviepilot userid 和 groupid
groupmod -o -g "${PGID}" moviepilot
usermod -o -u "${PUID}" moviepilot

# 更改文件权限
chown -R moviepilot:moviepilot \
    "${HOME}" \
    /app \
    /public \
    "${CONFIG_DIR}" \
    /var/lib/nginx \
    /var/log/nginx
chown moviepilot:moviepilot /etc/hosts /tmp

# 启动前优先确认主运行环境仍然健康，避免插件依赖污染导致服务直接起不来。
ensure_backend_runtime_dependencies

# 下载浏览器内核
function install_browser_kernel() {
  local emulation="${BROWSER_EMULATION:-cloakbrowser}"
  emulation="${emulation,,}"
  local proxy="${HTTPS_PROXY:-${https_proxy:-$PROXY_HOST}}"

  if [ "${emulation}" != "cloakbrowser" ] && [ "${emulation}" != "flaresolverr" ] && [ -n "${emulation}" ]; then
    WARN "浏览器仿真类型 ${emulation} 已按 CloakBrowser 处理。"
  fi

  INFO "下载 CloakBrowser 浏览器内核"
  if [[ "$proxy" =~ ^https?:// ]]; then
    HTTPS_PROXY="$proxy" gosu moviepilot:moviepilot python -m cloakbrowser install
  else
    gosu moviepilot:moviepilot python -m cloakbrowser install
  fi
}

install_browser_kernel

# 证书管理
source /app/docker/cert.sh

# 启动前端nginx服务
INFO "→ 启动前端nginx服务..."
nginx

# 捕获信号并跳转到函数
trap 'graceful_exit 130 "signal"' SIGINT
trap 'graceful_exit 143 "signal"' SIGTERM

# 启动docker http proxy nginx
if [ -S "/var/run/docker.sock" ]; then
    INFO "→ 启动 Docker Proxy..."
    nginx -c /etc/nginx/docker_http_proxy.conf
    # 上面nginx是通过root启动的，会将目录权限改成root，所以需要重新再设置一遍权限
    chown -R moviepilot:moviepilot \
        /var/lib/nginx \
        /var/log/nginx
fi

# 设置后端服务权限掩码
umask "${UMASK}"

# 启动后端服务
INFO "→ 启动后端服务..."
if [ "${START_NOGOSU:-false}" = "true" ]; then
    "${VENV_PATH}/bin/python3" app/main.py > /dev/stdout 2> /dev/stderr &
else
    gosu moviepilot:moviepilot "${VENV_PATH}/bin/python3" app/main.py > /dev/stdout 2> /dev/stderr &
fi
PYTHON_PID=$!

# 等待 Python 进程退出。
# 如果收到信号，trap 会中断 wait，并执行 graceful_exit。
# 如果 Python 正常退出，wait 会结束，然后我们手动调用 graceful_exit。
wait "$PYTHON_PID" 2>/dev/null
exit_code=$?

# 如果 Python 自己退出了（非信号触发），执行清理
INTENTIONAL_RESTART_FLAG="${CONFIG_DIR}/temp/moviepilot.intentional_restart"
if [ -f "${INTENTIONAL_RESTART_FLAG}" ]; then
    rm -f "${INTENTIONAL_RESTART_FLAG}"
    restart_exit_code="$exit_code"
    if [ "$restart_exit_code" -eq 0 ]; then
        restart_exit_code=1
    fi
    WARN "→ 检测到内置手动重启标记，退出容器并交给 Docker 重启策略处理..."
    graceful_exit "$restart_exit_code" "intentional_restart"
fi

if [ "$exit_code" -eq 0 ] || [ "$exit_code" -eq 130 ] || [ "$exit_code" -eq 143 ]; then
    graceful_exit "$exit_code" "python_exit"
fi

diagnostic_keepalive "$exit_code"
