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

# 仅当启用HTTPS且需要自动签发时执行
if [ "$ENABLE_SSL" = "true" ] && [ "$AUTO_ISSUE_CERT" = "true" ]; then
    INFO "▄■▀▄■▀▄■▀▄■▀▄■▀ 证书管理开始 ▀■▄▀■▄▀■▄▀■▄▀■▄"

    # 创建证书目录
    mkdir -p /config/certs/"${SSL_DOMAIN}"
    chown moviepilot:moviepilot /config/certs -R

    # 安装acme.sh
    if [ ! -d "/config/acme.sh" ]; then
        INFO "→ 安装acme.sh..."
        git clone https://github.com/acmesh-official/acme.sh.git /config/acme.sh
        cd /config/acme.sh
        ./acme.sh --install --home /config/acme.sh \
            --config-home /config/acme.sh/data \
            --cert-home /config/certs \
            --accountemail "${SSL_EMAIL}"
    fi

    # 签发证书（仅当证书不存在时）
    if [ ! -f "/config/certs/${SSL_DOMAIN}/fullchain.pem" ]; then
        # 检查必要参数
        [ -z "${DNS_PROVIDER}" ] && { ERROR "必须指定DNS_PROVIDER环境变量"; exit 1; }
        [ -z "${SSL_DOMAIN}" ] && { ERROR "必须指定SSL_DOMAIN环境变量"; exit 1; }

        INFO "→ 签发证书: ${SSL_DOMAIN} (DNS验证方式: ${DNS_PROVIDER})"

        # 导出所有ACME_ENV_开头的环境变量（自动去除前缀）
        INFO "正在加载ACME环境变量..."
        for acme_var in $(env | grep '^ACME_ENV_'); do
            key="${acme_var#ACME_ENV_}"
            key="${key%%=*}"
            value="${acme_var#ACME_ENV_${key}=}"
            export "${key}=${value}"
            INFO "已加载环境变量: ${key}=******"
        done

        # 签发证书
        /config/acme.sh/acme.sh --issue \
            --dns "${DNS_PROVIDER}" \
            --domain "${SSL_DOMAIN}" \
            --key-file /config/certs/"${SSL_DOMAIN}"/privkey.pem \
            --fullchain-file /config/certs/"${SSL_DOMAIN}"/fullchain.pem \
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
fi