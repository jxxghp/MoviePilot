#!/bin/bash

set -e

cd /app
umask "${UMASK:-000}"

# 在导入原生扩展前启用崩溃诊断，线程栈经 stderr 交给 supervisor 收集。
if [ "${START_NOGOSU:-false}" = "true" ]; then
    exec "${VENV_PATH:-/opt/venv}/bin/python3" -X faulthandler app/main.py
fi

exec gosu moviepilot:moviepilot "${VENV_PATH:-/opt/venv}/bin/python3" -X faulthandler app/main.py
