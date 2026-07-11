#!/bin/bash
# 企业微信可信IP自动更新器 - 启动脚本

cd "$(dirname "$0")"

# 创建数据目录
mkdir -p data

# 使用虚拟环境 Python
if [ -d "venv" ]; then
    PYTHON="venv/bin/python"
else
    # 尝试系统 Python
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    echo "❌ 未找到 Python，请先运行 bash install.sh"
    exit 1
fi

PORT=${UPDATER_PORT:-18800}

echo "====================================="
echo " 企业微信可信IP自动更新器"
echo " 端口: $PORT"
echo " 地址: http://localhost:$PORT"
echo "====================================="

# 启动服务
exec $PYTHON main.py
