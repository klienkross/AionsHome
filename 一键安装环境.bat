@echo off
chcp 65001 >nul
title Aion Chat 环境安装
cd /d "%~dp0"

echo ========================================
echo   Aion Chat 环境一键安装
echo ========================================
echo.

:: ────────────────────────────────────────
:: 1. 检查 Python 是否已安装
:: ────────────────────────────────────────
echo [1/4] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ❌ 未检测到 Python！请先安装 Python 3.10 或更高版本。
    echo.
    echo    下载地址: https://www.python.org/downloads/
    echo    安装时请务必勾选 "Add Python to PATH" ！
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo    ✅ 检测到 Python %PYVER%

:: ────────────────────────────────────────
:: 2. 检查 venv 模块 + 创建虚拟环境
:: ────────────────────────────────────────
echo.
echo [2/5] 检查虚拟环境模块...
python -c "import venv" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ❌ Python 的 venv 模块不可用！
    echo.
    echo    这通常是因为你的 Python 从 Microsoft Store 安装的。
    echo    请按以下步骤修复：
    echo    1. 卸载 Microsoft Store 版本的 Python
    echo       （设置 → 应用 → 搜索 Python → 卸载）
    echo    2. 从官网重新下载安装：https://www.python.org/downloads/
    echo    3. 安装时选 Customize installation，确保所有组件都勾选
    echo    4. 重新运行本脚本
    echo.
    pause
    exit /b 1
)
echo    ✅ venv 模块正常

echo.
echo [3/5] 创建虚拟环境 (.venv)...
set "NEED_VENV=1"
if exist ".venv\Scripts\activate.bat" (
    findstr /i /c:"%CD%" ".venv\Scripts\activate.bat" >nul 2>&1
    if not errorlevel 1 (
        set "NEED_VENV=0"
        echo    虚拟环境已存在且路径正确，跳过创建
    ) else (
        echo    虚拟环境路径不匹配（可能是从别处复制的），正在重建...
        rmdir /s /q .venv >nul 2>&1
    )
)
if "%NEED_VENV%"=="1" (
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ❌ 创建虚拟环境失败！
        echo    请确保 Python 是从 python.org 官网安装的（非 Microsoft Store 版本）
        echo    安装时请选择 Customize installation 并勾选所有组件
        pause
        exit /b 1
    )
    echo    虚拟环境创建成功
)

:: ────────────────────────────────────────
:: 3. 安装依赖
:: ────────────────────────────────────────
echo.
echo [4/5] 安装 Python 依赖包（首次可能需要几分钟）...
.venv\Scripts\pip install -r aion-chat\requirements.txt -q
if errorlevel 1 (
    echo.
    echo ❌ 依赖安装失败！
    echo.
    echo    常见原因及解决方法：
    echo.
    echo    1. 网络问题 → 尝试使用国内镜像：
    echo       .venv\Scripts\pip install -r aion-chat\requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
    echo.
    echo    2. 缺少 C++ 编译工具 → 如果报错包含 "Microsoft Visual C++ 14.0 or greater is required"：
    echo       请下载安装 Microsoft C++ Build Tools：
    echo       https://visualstudio.microsoft.com/zh-hans/visual-cpp-build-tools/
    echo       安装时勾选「使用 C++ 的桌面开发」，装完重启电脑后再试
    echo.
    pause
    exit /b 1
)
echo    ✅ 所有依赖安装完成

:: ────────────────────────────────────────
:: 5. 完成
:: ────────────────────────────────────────
echo.
echo [5/5] 检查安装结果...
.venv\Scripts\python -c "import fastapi; print('    FastAPI', fastapi.__version__)"
.venv\Scripts\python -c "import cv2; print('    OpenCV ', cv2.__version__)"
.venv\Scripts\python -c "import numpy; print('    NumPy  ', numpy.__version__)"
.venv\Scripts\python -c "import pyncm; print('    PyNCM   OK')"
.venv\Scripts\python -c "import psutil; print('    psutil ', psutil.__version__)"
.venv\Scripts\python -c "import ebooklib; print('    ebooklib OK')"
.venv\Scripts\python -c "import bs4; print('    BeautifulSoup4 OK')"

echo.
echo ========================================
echo   ✅ 环境安装完成！
echo   现在可以双击「一键启动.bat」运行了
echo ========================================
echo.
pause
