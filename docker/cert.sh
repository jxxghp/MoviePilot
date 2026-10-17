#!/bin/bash
CERT_ERREXIT_WAS_SET=false
CERT_PIPEFAIL_WAS_SET=false
if [[ "$-" == *e* ]]; then
    CERT_ERREXIT_WAS_SET=true
fi
if shopt -qo pipefail; then
    CERT_PIPEFAIL_WAS_SET=true
fi
set -e
set -o pipefail

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

CERT_DIR="${CONFIG_DIR}/certs"
ACME_HOME="${CONFIG_DIR}/acme.sh"
ACME_DATA_DIR="${ACME_HOME}/data"
ACME_CERT_DIR="${CERT_DIR}/${SSL_DOMAIN}"
ACME_LATEST_CERT_DIR="${CERT_DIR}/latest"
NGINX_RELOAD_CMD="nginx -s reload 2>/dev/null || true"

# 恢复调用方原有 shell 选项，避免 source 本脚本后影响 entrypoint 后续流程。
function restore_shell_options() {
    if ! ${CERT_PIPEFAIL_WAS_SET}; then
        set +o pipefail
    fi
    if ! ${CERT_ERREXIT_WAS_SET}; then
        set +e
    fi
}

# 输出错误并恢复调用方 shell 选项，确保 source 失败时不会污染后续流程。
function exit_with_error() {
    ERROR "$1"
    restore_shell_options
    exit 1
}

# 使用固定的 acme.sh 工作目录执行命令，避免签发、安装和续期读取不同配置目录。
function run_acme() {
    LE_WORKING_DIR="${ACME_HOME}" \
        LE_CONFIG_HOME="${ACME_DATA_DIR}" \
        LE_CERT_HOME="${CERT_DIR}" \
        "${ACME_HOME}/acme.sh" --home "${ACME_HOME}" "$@"
}

# 维护 nginx 使用的稳定证书目录链接，兼容手动证书和自动签发证书路径。
function link_latest_cert_dir() {
    if [ -e "${ACME_LATEST_CERT_DIR}" ] && [ ! -L "${ACME_LATEST_CERT_DIR}" ]; then
        rm -rf "${ACME_LATEST_CERT_DIR}"
    fi
    ln -sfn "${ACME_CERT_DIR}" "${ACME_LATEST_CERT_DIR}"
}

# 配置证书自动续期；续期任务失败不应阻断已有证书启动。
function configure_cert_renewal() {
    if ! command -v cron >/dev/null 2>&1; then
        WARN "未安装 cron，跳过证书自动续期任务配置"
        return 0
    fi

    if ! mkdir -p /etc/cron.d 2>/dev/null; then
        WARN "无法创建 /etc/cron.d，跳过证书自动续期任务配置"
        return 0
    fi

    if ! printf "0 3 * * * root LE_WORKING_DIR=%q LE_CONFIG_HOME=%q LE_CERT_HOME=%q %q --cron --home %q\n" \
        "${ACME_HOME}" \
        "${ACME_DATA_DIR}" \
        "${CERT_DIR}" \
        "${ACME_HOME}/acme.sh" \
        "${ACME_HOME}" > /etc/cron.d/acme 2>/dev/null; then
        WARN "无法写入 /etc/cron.d/acme，跳过证书自动续期任务配置"
        return 0
    fi

    if ! chmod 644 /etc/cron.d/acme 2>/dev/null; then
        WARN "无法设置 /etc/cron.d/acme 权限，证书自动续期任务可能不会生效"
        return 0
    fi

    if ! pgrep -x cron >/dev/null 2>&1 && ! cron 2>/dev/null; then
        WARN "cron 启动失败，证书自动续期任务可能不会生效"
    fi
}

if [ "${ENABLE_SSL}" = "true" ] && \
   [ "${AUTO_ISSUE_CERT}" = "true" ] && \
   [ -n "${SSL_DOMAIN}" ]; then

    mkdir -p "${ACME_CERT_DIR}"
    if id moviepilot >/dev/null 2>&1; then
        chown moviepilot:moviepilot "${CERT_DIR}" -R
    fi

    # 安装acme.sh（使用官方安装脚本）
    if [ ! -f "${ACME_HOME}/acme.sh" ]; then
        INFO "→ 安装acme.sh..."

        # 安装命令失败时退出签发流程，避免继续调用不存在的 acme.sh。
        INFO "正在下载并安装 acme.sh..."
        
        install_args=("--install-online")
        if [ -n "${SSL_EMAIL}" ]; then
            install_args+=("--accountemail" "${SSL_EMAIL}")
        else
            WARN "未设置SSL_EMAIL，建议配置邮箱用于证书过期提醒"
        fi
        
        if ! curl -sSL https://get.acme.sh | \
            LE_WORKING_DIR="${ACME_HOME}" \
            LE_CONFIG_HOME="${ACME_DATA_DIR}" \
            LE_CERT_HOME="${CERT_DIR}" \
            sh -s -- "${install_args[@]}"; then
            exit_with_error "acme.sh 安装失败"
        fi

        if [ ! -f "${ACME_HOME}/acme.sh" ]; then
            exit_with_error "acme.sh 安装后文件不存在，安装可能失败"
        fi

        INFO "acme.sh 安装成功"
    fi

    # 签发证书（仅当证书不存在时）
    if [ ! -f "${ACME_CERT_DIR}/fullchain.pem" ] || [ ! -f "${ACME_CERT_DIR}/privkey.pem" ]; then
        REQUIRED_VARS=("DNS_PROVIDER")
        for var in "${REQUIRED_VARS[@]}"; do
            eval "value=\${${var}}"
            [ -z "$value" ] && exit_with_error "必须设置环境变量: ${var}"
        done

        INFO "→ 签发证书: ${SSL_DOMAIN} (DNS验证方式: ${DNS_PROVIDER})"

        # 加载ACME环境变量（带安全过滤）
        acme_exported_vars=()
        acme_original_keys=()
        acme_original_values=()
        acme_had_original_values=()
        while IFS= read -r var_name; do
            [ -z "${var_name}" ] && continue
            key="${var_name#ACME_ENV_}"
            value="${!var_name}"

            # 过滤非法变量名
            if [[ "$key" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
                acme_original_keys+=("${key}")
                if eval "[ -n \"\${${key}+x}\" ]"; then
                    acme_had_original_values+=("true")
                    acme_original_values+=("$(eval "printf '%s' \"\${${key}}\"")")
                else
                    acme_had_original_values+=("false")
                    acme_original_values+=("")
                fi
                export "$key"="$value"
                acme_exported_vars+=("${key}")
            else
                WARN "跳过无效变量名: ${key}"
            fi
        done < <(compgen -A variable ACME_ENV_ || true)

        # 签发失败时恢复调用方的 shell 选项，再向入口返回失败。
        INFO "正在签发证书..."
        if ! run_acme --issue \
            --dns "${DNS_PROVIDER}" \
            --domain "${SSL_DOMAIN}" \
            --force; then
            exit_with_error "证书签发失败"
        fi

        INFO "正在安装证书文件..."
        if ! run_acme --install-cert \
            --domain "${SSL_DOMAIN}" \
            --key-file "${ACME_CERT_DIR}/privkey.pem" \
            --fullchain-file "${ACME_CERT_DIR}/fullchain.pem" \
            --reloadcmd "${NGINX_RELOAD_CMD}"; then
            exit_with_error "证书安装失败"
        fi

        for index in "${!acme_original_keys[@]}"; do
            var_name="${acme_original_keys[$index]}"
            if [ "${acme_had_original_values[$index]}" = "true" ]; then
                export "${var_name}=${acme_original_values[$index]}"
            else
                unset "${var_name}"
            fi
        done

        link_latest_cert_dir
        INFO "证书签发成功"
    else
        link_latest_cert_dir
        INFO "证书已存在，跳过签发步骤"
    fi

    INFO "→ 配置cron自动更新..."
    configure_cert_renewal

elif [ "${ENABLE_SSL}" = "true" ] && [ "${AUTO_ISSUE_CERT}" = "true" ] && [ -z "${SSL_DOMAIN}" ]; then
    exit_with_error "已启用自动签发证书但未设置 SSL_DOMAIN，无法生成 HTTPS 证书"
elif [ "${ENABLE_SSL}" = "true" ] && [ "${AUTO_ISSUE_CERT}" = "false" ]; then
    INFO "SSL已启用但自动签发证书已禁用，将使用手动配置的证书"
    if [ -f "${ACME_LATEST_CERT_DIR}/fullchain.pem" ] && [ -f "${ACME_LATEST_CERT_DIR}/privkey.pem" ]; then
        INFO "检测到证书文件，SSL配置正常"
    else
        exit_with_error "未检测到证书文件，请将 fullchain.pem 和 privkey.pem 放入 ${ACME_LATEST_CERT_DIR}"
    fi
fi

restore_shell_options
