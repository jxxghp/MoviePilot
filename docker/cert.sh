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
if [ "$ENABLE_SSL" = "true" ] && \
   [ "$AUTO_ISSUE_CERT" = "true" ] && \
   [ -n "$SSL_DOMAIN" ]; then

    INFO "▄■▀▄■▀▄■▀▄■▀▄■▀ 证书管理开始 ▀■▄▀■▄▀■▄▀■▄▀■▄"

    # 创建证书目录
    mkdir -p /config/certs/"${SSL_DOMAIN}"
    chown moviepilot:moviepilot /config/certs -R

    # 安装acme.sh（使用官方安装脚本）
    if [ ! -d "/config/acme.sh" ]; then
        INFO "→ 安装acme.sh..."

        # 生成安装参数
        INSTALL_ARGS=(
            "--install-online"
            "--home" "/config/acme.sh"
            "--config-home" "/config/acme.sh/data"
            "--cert-home" "/config/certs"
        )

        # 添加邮箱参数（如果设置）
        if [ -n "$SSL_EMAIL" ]; then
            INSTALL_ARGS+=("--accountemail" "$SSL_EMAIL")
        else
            WARN "未设置SSL_EMAIL，建议配置邮箱用于证书过期提醒"
        fi

        # 执行官方安装命令
        curl -sSL https://get.acme.sh | sh -s -- "${INSTALL_ARGS[@]}"
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

        # 签发证书
        /config/acme.sh/acme.sh --issue \
            --dns "${DNS_PROVIDER}" \
            --domain "${SSL_DOMAIN}" \
            --key-file /config/certs/"${SSL_DOMAIN}"/privkey.pem \
            --fullchain-file /config/certs/"${SSL_DOMAIN}"/fullchain.pem \
            --reloadcmd "nginx -s reload" \
            --force

        # 创建稳定符号链接
        ln -sf /config/certs/"${SSL_DOMAIN}" /config/certs/latest
    fi

    # 配置自动更新任务
    INFO "→ 配置cron自动更新..."
    echo "0 3 * * * /config/acme.sh/acme.sh --cron --home /config/acme.sh && nginx -s reload" > /etc/cron.d/acme
    chmod 644 /etc/cron.d/acme
    service cron start

    INFO "▄■▀▄■▀▄■▀▄■▀▄■▀ 证书管理完成 ▀■▄▀■▄▀■▄▀■▄▀■▄"

elif [ "$ENABLE_SSL" = "true" ] && [ "$AUTO_ISSUE_CERT" = "true" ] && [ -z "$SSL_DOMAIN" ]; then
    WARN "已启用自动签发证书但未设置SSL_DOMAIN，跳过证书管理"
fi