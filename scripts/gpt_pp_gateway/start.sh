#!/bin/bash

# =================================================================
# Plus PayPal 智能收单网关一键本地启动脚本 (macOS / Linux)
# =================================================================

# 确保脚本发生错误时退出
set -e

# 获取脚本所在根目录
ROOT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$ROOT_DIR"

echo "================================================================="
echo "⚙️  正在初始化 Plus PayPal 本地一键启动包..."
echo "================================================================="

# 1. 自动准备环境配置文件
if [ ! -f .env ]; then
    echo "📝 未检测到 .env 配置文件，正在根据模板自动生成..."
    cp .env.example .env
fi

# 2. 检查并创建虚拟环境
if [ ! -d .venv ]; then
    echo "📦 正在为您创建 Python 虚拟环境 (.venv)..."
    python3 -m venv .venv
fi

# 激活虚拟环境
echo "🔌 正在激活虚拟环境..."
source .venv/bin/activate

# 3. 安装/升级依赖项目
echo "⚡ 正在安装网关运行所需 Python 依赖库..."
pip install --upgrade pip -q
pip install curl_cffi playwright -q

# 4. 确保 Playwright 浏览器组件已安装
echo "🌐 正在下载并配置 Playwright 浏览器内核..."
playwright install chromium
# 如果系统缺少底层依赖，尝试安装（非必须，失败不阻断）
playwright install-deps || true

# 5. 自动启动浏览器打开控制台首页
echo "🚀 正在自动开启默认浏览器并准备载入控制台..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    (sleep 2.5 && open "http://127.0.0.1:8888") &
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    # Linux
    (sleep 2.5 && xdg-open "http://127.0.0.1:8888") &
fi

# 6. 启动网关主服务
echo "🔥 网关常驻引擎已高强度起航，监听地址: http://127.0.0.1:8888"
python3 webapp/server.py --host 127.0.0.1 --port 8888
