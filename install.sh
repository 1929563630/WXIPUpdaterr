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

# ---------- 镜像配置（国内加速） ----------
PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_HOST="pypi.tuna.tsinghua.edu.cn"
PLAYWRIGHT_HOST="https://npmmirror.com/mirrors/playwright"

echo "🌐 配置 pip 国内镜像..."
mkdir -p ~/.config/pip
cat > ~/.config/pip/pip.conf << EOF
[global]
index-url = ${PIP_INDEX}
trusted-host = ${PIP_HOST}
timeout = 120
EOF
# 同时写入系统级配置，兼容 root 运行
if [ -w /etc ] || [ "$(id -u)" = "0" ]; then
    cat > /etc/pip.conf << EOF
[global]
index-url = ${PIP_INDEX}
trusted-host = ${PIP_HOST}
timeout = 120
EOF
fi

# 设置 Playwright 浏览器下载镜像
export PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_HOST}"
echo "✅ 镜像配置完成"
echo ""

# ---------- 检测 Python ----------
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

# ---------- 创建虚拟环境 ----------
if [ ! -d "venv" ]; then
    echo "📦 创建虚拟环境..."
    $PYTHON -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate
echo "✅ 虚拟环境已激活"

# ---------- 安装依赖 ----------
echo "📦 安装 Python 依赖（使用国内镜像）..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "✅ Python 依赖已安装"

# ---------- 安装 Playwright 浏览器 ----------
echo "📦 安装 Playwright Chromium（约200MB，走国内镜像）..."
if playwright install chromium --with-deps 2>/dev/null; then
    echo "✅ Chromium 已安装（含系统依赖）"
else
    echo "⚠️  带系统依赖安装失败，尝试仅安装浏览器..."
    playwright install chromium
    echo "✅ Chromium 已安装"
fi

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
