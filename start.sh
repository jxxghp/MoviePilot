#!/bin/bash

# 使用 `envsubst` 将模板文件中的 ${NGINX_PORT} 替换为实际的环境变量值
envsubst '${NGINX_PORT}' < /etc/nginx/nginx.template.conf > /etc/nginx/nginx.conf
# 更改 moviepilot userid 和 groupid
groupmod -o -g ${PGID} moviepilot
usermod -o -u ${PUID} moviepilot
# 更改文件权限
chown -R moviepilot:moviepilot ${HOME} /app /config /etc/hosts
# 下载浏览器内核
gosu moviepilot:moviepilot playwright install chromium
# 启动前端nginx服务
nginx
# 设置后端服务权限掩码
umask ${UMASK}
# 启动后端服务
exec gosu moviepilot:moviepilot python3 app/main.py
