#!/bin/sh

umask ${UMASK}
playwright install chromium
python3 app/main.py
