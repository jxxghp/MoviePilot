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

# 校正设置目录
CONFIG_DIR="${CONFIG_DIR:-/config}"

# 记录非系统环境（docker容器表）提供的变量
declare -ga VARS_SET_BY_SCRIPT=()

# 环境变量补全
# 优先级: 系统环境变量 -> .env 文件 (即使为空字符串) -> 预设默认值
# 精准适配 Python 端 set_key (quote_mode="always", 单引号包裹, \' 转义)
function load_config_from_app_env() {

    local env_file="${CONFIG_DIR}/app.env"

    # 定义 ["变量名"]="预设默认值"
    # 禁止填入 CONFIG_DIR 变量，ACME_ENV_ 开头的变量暂时不处理，还是交由 cert.sh 处理
    declare -A vars_and_default_values=(
        # update.sh
        ["PIP_PROXY"]=""
        ["GITHUB_PROXY"]=""
        ["PROXY_HOST"]=""
        ["GITHUB_TOKEN"]=""
        ["MOVIEPILOT_AUTO_UPDATE"]="release"

        # cert
        ["ENABLE_SSL"]="false"
        ["SSL_DOMAIN"]=""
        ["NGINX_PORT"]="3000"
        ["PORT"]="3001"
        ["NGINX_CLIENT_MAX_BODY_SIZE"]="10m"
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

                if [[ -n "${vars_and_default_values[$key_in_file]+_}" ]]; then
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

    INFO "正在根据优先级确定并导出配置值..."
    for var_name in "${!vars_and_default_values[@]}"; do
        local fallback_value="${vars_and_default_values[$var_name]}"
        local final_value
        local value_source="未设置"
        # 标志变量是否来自初始环境
        local set_by_initial_env=false

        # 检查变量是否在环境中已设置（可能为空）
        if eval "[ -n \"\${${var_name}+x}\" ]"; then
            # 获取其值
            final_value="$(eval echo \"\$"${var_name}"\")"
            value_source="系统环境变量"
            set_by_initial_env=true
        elif [[ -n "${values_from_env_file["${var_name}"]+_}" ]]; then
            final_value="${values_from_env_file["${var_name}"]}"
            value_source=".env 文件"
        else
            final_value="${fallback_value}"
            value_source="内置默认值"
        fi

        # 不论来源如何，都导出变量，以便脚本的其余部分和子进程使用
        # (例如 envsubst, mp_update.sh, cert.sh)
        if declare -gx "${var_name}=${final_value}"; then
            if [ -z "${final_value}" ]; then
                 INFO "变量 ${var_name}, 值为空, 来源: ${value_source})。"
            else
                 INFO "变量 ${var_name}, 值: ${final_value} , (来源: ${value_source})。"
            fi

            # 如果变量不是来自初始环境变量，则记录下来以便稍后 unset
            if ! ${set_by_initial_env}; then
                # 检查是否已在数组中，避免重复添加
                local found_in_script_vars=false
                for item in "${VARS_SET_BY_SCRIPT[@]}"; do
                    if [[ "$item" == "$var_name" ]]; then
                        found_in_script_vars=true
                        break
                    fi
                done
                if ! ${found_in_script_vars}; then
                    VARS_SET_BY_SCRIPT+=("${var_name}")
                fi
            fi
        else
            ERROR "导出变量 ${var_name} (值: '${final_value}', 来源: ${value_source}) 失败。"
        fi
    done

    shopt -u extglob
    INFO "配置加载流程执行完毕。"
}

# 生成HTTPS配置块
if [ "${ENABLE_SSL}" = "true" ]; then
    export HTTPS_SERVER_CONF=$(cat <<EOF
    server {
        include /etc/nginx/mime.types;
        default_type application/octet-stream;

        listen 443 ssl;
        listen [::]:443 ssl;
        server_name ${SSL_DOMAIN:-moviepilot};

        # SSL证书路径
        ssl_certificate "${CONFIG_DIR}"/certs/latest/fullchain.pem;
        ssl_certificate_key "${CONFIG_DIR}"/certs/latest/privkey.pem;

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
    export HTTPS_SERVER_CONF="# HTTPS未启用"
fi

# 使用 `envsubst` 将模板文件中的 ${NGINX_PORT} 替换为实际的环境变量值
envsubst '${NGINX_PORT}${PORT}${NGINX_CLIENT_MAX_BODY_SIZE}${ENABLE_SSL}${HTTPS_SERVER_CONF}' < /etc/nginx/nginx.template.conf > /etc/nginx/nginx.conf
# 自动更新
cd /
source /usr/local/bin/mp_update.sh
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
# 下载浏览器内核
if [[ "$HTTPS_PROXY" =~ ^https?:// ]] || [[ "$HTTPS_PROXY" =~ ^https?:// ]] || [[ "$PROXY_HOST" =~ ^https?:// ]]; then
  HTTPS_PROXY="${HTTPS_PROXY:-${https_proxy:-$PROXY_HOST}}" gosu moviepilot:moviepilot playwright install chromium
else
  gosu moviepilot:moviepilot playwright install chromium
fi
# 证书管理
source /app/docker/cert.sh
# 启动前端nginx服务
INFO "→ 启动前端nginx服务..."
nginx
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

# 清除非系统环境导入的变量，保证转移到 dumb-init 的时候，不会带入不必要的环境变量
INFO "准备为 Python 应用清理的非系统环境导入的变量..."
if [ ${#VARS_SET_BY_SCRIPT[@]} -gt 0 ]; then
    for var_to_unset in "${VARS_SET_BY_SCRIPT[@]}"; do
        # 再次确认变量确实存在于当前环境中（虽然理论上应该存在）
        if eval "[ -n \"\${${var_to_unset}+x}\" ]"; then
            INFO "取消设置环境变量: ${var_to_unset}"
            unset "${var_to_unset}"
        else
            WARN "变量 ${var_to_unset} 已不存在，无需取消设置。"
        fi
    done
else
    INFO "没有由非系统环境导入的变量需要清理。"
fi

# 启动后端服务
INFO "→ 启动后端服务..."
exec dumb-init gosu moviepilot:moviepilot python3 app/main.py
