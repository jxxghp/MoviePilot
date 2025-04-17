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

# 生成HTTPS配置块
if [ "$ENABLE_SSL" = "true" ]; then
    export HTTPS_SERVER_CONF=$(cat <<EOF
    server {
        include /etc/nginx/mime.types;
        default_type application/octet-stream;

        listen 443 ssl;
        listen [::]:443 ssl;
        server_name ${SSL_DOMAIN:-moviepilot};

        # SSL证书路径
        ssl_certificate /etc/ssl/certs/latest/fullchain.pem;
        ssl_certificate_key /etc/ssl/certs/latest/privkey.pem;

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
export NGINX_CLIENT_MAX_BODY_SIZE=${NGINX_CLIENT_MAX_BODY_SIZE:-10m}
envsubst '${NGINX_PORT}${PORT}${NGINX_CLIENT_MAX_BODY_SIZE}${HTTPS_SERVER_CONF}' < /etc/nginx/nginx.template.conf > /etc/nginx/nginx.conf
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
    /config \
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
# 启动后端服务
INFO "→ 启动后端服务..."
exec dumb-init gosu moviepilot:moviepilot python3 app/main.py
