#!/usr/bin/sh

umask ${UMASK}
echo "开始下载浏览器 ..."
playwright install --with-deps chromium
python3 app/main.py
