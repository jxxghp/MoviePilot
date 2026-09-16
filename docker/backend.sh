#!/bin/bash

set -e

cd /app
umask "${UMASK:-000}"

if [ "${START_NOGOSU:-false}" = "true" ]; then
    exec "${VENV_PATH:-/opt/venv}/bin/python3" app/main.py
fi

exec gosu moviepilot:moviepilot "${VENV_PATH:-/opt/venv}/bin/python3" app/main.py
