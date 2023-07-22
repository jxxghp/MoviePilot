#!/bin/sh

umask ${UMASK}
# 使用 `envsubst` 将模板文件中的 ${NGINX_PORT} 替换为实际的环境变量值
envsubst '${NGINX_PORT}' < /etc/nginx/nginx.template.conf > /etc/nginx/nginx.conf
# 下载浏览器内核
playwright install chromium
# 启动后端服务
python3 app/main.py
