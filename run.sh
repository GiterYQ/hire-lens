#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "🔭 正在启动 HireLens (聘镜) · 招聘端视角 AI 求职台"
echo "=========================================="

if [ ! -d ".venv" ]; then
    echo "📦 正在创建本地 Python 虚拟环境 (.venv)..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "🔄 检查/安装必要依赖..."
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
    echo "💡 未检测到 .env 配置文件，自动从 .env.example 复制默认配置..."
    cp .env.example .env
fi

echo "🚀 服务启动中，浏览器访问: http://127.0.0.1:8765"
exec python3 web/app.py
