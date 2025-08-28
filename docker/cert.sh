#!/bin/bash
set -e

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

# 核心条件验证
if [ "${ENABLE_SSL}" = "true" ] && \
   [ "${AUTO_ISSUE_CERT}" = "true" ] && \
   [ -n "${SSL_DOMAIN}" ]; then

    # 创建证书目录
    mkdir -p /config/certs/"${SSL_DOMAIN}"
    chown moviepilot:moviepilot /config/certs -R

    # 安装acme.sh（使用官方安装脚本）
    if [ ! -d "/config/acme.sh" ]; then
        INFO "→ 安装acme.sh..."

        # 设置安装环境变量
        export LE_WORKING_DIR="/config/acme.sh"
        export LE_CONFIG_HOME="/config/acme.sh/data"
        export LE_CERT_HOME="/config/certs"

        # 执行官方安装命令（添加错误处理）
        INFO "正在下载并安装 acme.sh..."
        
        # 构建安装命令
        INSTALL_CMD="curl -sSL https://get.acme.sh | sh -s -- --install-online"
        if [ -n "${SSL_EMAIL}" ]; then
            INSTALL_CMD="${INSTALL_CMD} --accountemail ${SSL_EMAIL}"
        else
            WARN "未设置SSL_EMAIL，建议配置邮箱用于证书过期提醒"
        fi
        
        if ! eval "${INSTALL_CMD}"; then
            ERROR "acme.sh 安装失败"
            exit 1
        fi

        # 验证安装是否成功
        if [ ! -f "/config/acme.sh/acme.sh" ]; then
            ERROR "acme.sh 安装后文件不存在，安装可能失败"
            exit 1
        fi

        INFO "acme.sh 安装成功"
    fi

    # 签发证书（仅当证书不存在时）
    if [ ! -f "/config/certs/${SSL_DOMAIN}/fullchain.pem" ]; then
        # 必要参数检查
        REQUIRED_VARS=("DNS_PROVIDER")
        for var in "${REQUIRED_VARS[@]}"; do
            eval "value=\${${var}}"
            [ -z "$value" ] && { ERROR "必须设置环境变量: ${var}"; exit 1; }
        done

        INFO "→ 签发证书: ${SSL_DOMAIN} (DNS验证方式: ${DNS_PROVIDER})"

        # 加载ACME环境变量（带安全过滤）
        INFO "正在加载ACME环境变量..."
        env | grep '^ACME_ENV_' | while read -r line; do
            key="${line#ACME_ENV_}"
            key="${key%%=*}"
            value="${line#ACME_ENV_${key}=}"

            # 过滤非法变量名
            if [[ "$key" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
                export "$key"="$value"
                INFO "已加载环境变量: ${key}=******"
            else
                WARN "跳过无效变量名: ${key}"
            fi
        done

        # 签发证书（添加错误处理）
        INFO "正在签发证书..."
        if ! /config/acme.sh/acme.sh --issue \
            --dns "${DNS_PROVIDER}" \
            --domain "${SSL_DOMAIN}" \
            --key-file /config/certs/"${SSL_DOMAIN}"/privkey.pem \
            --fullchain-file /config/certs/"${SSL_DOMAIN}"/fullchain.pem \
            --reloadcmd "nginx -s reload" \
            --force; then
            ERROR "证书签发失败"
            exit 1
        fi

        # 创建稳定符号链接
        ln -sf /config/certs/"${SSL_DOMAIN}" /config/certs/latest
        INFO "证书签发成功"
    else
        INFO "证书已存在，跳过签发步骤"
    fi

    # 配置自动更新任务
    INFO "→ 配置cron自动更新..."
    echo "0 3 * * * /config/acme.sh/acme.sh --cron --home /config/acme.sh && nginx -s reload" > /etc/cron.d/acme
    chmod 644 /etc/cron.d/acme
    service cron start

elif [ "${ENABLE_SSL}" = "true" ] && [ "${AUTO_ISSUE_CERT}" = "true" ] && [ -z "${SSL_DOMAIN}" ]; then
    WARN "已启用自动签发证书但未设置SSL_DOMAIN，跳过证书管理"
elif [ "${ENABLE_SSL}" = "true" ] && [ "${AUTO_ISSUE_CERT}" = "false" ]; then
    INFO "SSL已启用但自动签发证书已禁用，将使用手动配置的证书"
    # 检查证书文件是否存在
    if [ -f "/config/certs/latest/fullchain.pem" ] && [ -f "/config/certs/latest/privkey.pem" ]; then
        INFO "检测到证书文件，SSL配置正常"
    else
        WARN "未检测到证书文件，请确保手动配置了正确的证书路径"
    fi
fi