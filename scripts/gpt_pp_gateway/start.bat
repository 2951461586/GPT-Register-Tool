@echo off
:: =================================================================
:: Plus PayPal 智能收单网关一键本地启动脚本 (Windows)
:: =================================================================
title ChatGPT Extract Payment Link Start Loader
chcp 65001 >nul

cd /d "%~dp0"

echo =================================================================
echo ⚙️  正在初始化 Plus PayPal 本地一键启动包...
echo =================================================================

:: 1. 自动准备环境配置文件
if not exist .env (
    echo 📝 未检测到 .env 配置文件，正在根据模板自动生成...
    copy .env.example .env >nul
)

:: 2. 检查并创建虚拟环境
if not exist .venv (
    echo 📦 正在为您创建 Python 虚拟环境 (.venv)...
    python -m venv .venv
)

:: 激活虚拟环境
echo 🔌 正在激活虚拟环境...
call .venv\Scripts\activate.bat

:: 3. 安装/升级依赖项目
echo ⚡ 正在安装网关运行所需 Python 依赖库...
python -m pip install --upgrade pip -q
pip install curl_cffi playwright -q

:: 4. 确保 Playwright 浏览器组件已安装
echo 🌐 正在下载并配置 Playwright 浏览器内核...
playwright install chromium

:: 5. 自动启动浏览器打开控制台首页
echo 🚀 正在自动开启默认浏览器并准备载入控制台...
start http://127.0.0.1:8888

:: 6. 启动网关主服务
echo 🔥 网关常驻引擎已高强度起航，监听地址: http://127.0.0.1:8888
python webapp/server.py --host 127.0.0.1 --port 8888

pause
