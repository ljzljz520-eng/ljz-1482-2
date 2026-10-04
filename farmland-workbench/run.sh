#!/usr/bin/env bash
# 启动航拍农田报告工作台 (纯 Python 标准库, 无外部依赖)
set -e
cd "$(dirname "$0")/server"
PORT="${PORT:-8080}" exec python3 app.py
