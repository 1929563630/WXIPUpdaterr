#!/bin/bash
# ============================================
# 企业微信可信IP自动更新器 - 一键安装脚本
# 支持 Ubuntu/Debian/CentOS/macOS
# ============================================

set -e
cd "$(dirname "$0")"

echo "====================================="
echo "  企业微信可信IP自动更新器 - 安装向导"
echo "====================================="
echo ""

# 检测 Python
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ 未找到 Python 3.10+，请先安装："
    echo "   Ubuntu/Debian: sudo apt install python3 python3-pip python3-venv"
    echo "   CentOS/RHEL:   sudo yum install python3 python3-pip"
    echo "   macOS:         brew install python@3.11"
    exit 1
fi

echo "✅ Python: $($PYTHON --version)"

# 创建虚拟环境（如果不存在）
if [ ! -d "venv" ]; then
    echo "📦 创建虚拟环境..."
    $PYTHON -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate
echo "✅ 虚拟环境已激活"

# 安装依赖
echo "📦 安装 Python 依赖..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "✅ Python 依赖已安装"

# 安装 Playwright 浏览器
echo "📦 安装 Playwright Chromium（首次需要，约200MB）..."
playwright install chromium --with-deps 2>/dev/null || playwright install chromium
echo "✅ Chromium 已安装"

echo ""
echo "====================================="
echo "  ✅ 安装完成！"
echo "====================================="
echo ""
echo "启动方式："
echo "  bash start.sh"
echo ""
echo "然后浏览器打开："
echo "  http://localhost:18800"
echo ""
