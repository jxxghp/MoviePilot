#!/bin/sh

umask ${UMASK}
playwright install --with-deps chromium
python3 app/main.py
