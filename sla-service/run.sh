#!/usr/bin/env bash
# 启动工单时限计时服务（后端托管前端构建产物）
set -e
cd "$(dirname "$0")/backend"
exec ../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
