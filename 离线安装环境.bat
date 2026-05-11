@echo off
chcp 65001 >nul
title Aion Chat 离线安装（无需联网）
cd /d "%~dp0"

echo ════════════════════════════════════════
echo   Aion Chat 离线一键安装
echo   （所有依赖已内置，无需联网）
echo ════════════════════════════════════════
echo.

:: ────────────────────────────────────────
:: 1. 检查 Python 是否已安装
:: ────────────────────────────────────────
echo [1/5] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ╔══════════════════════════════════════════════╗
    echo ║  未检测到 Python！请先安装 Python            ║
    echo ║                                              ║
    echo ║  推荐版本: Python 3.10 ~ 3.13                ║
    echo ║  下载地址: https://www.python.org/downloads/  ║
    echo ║                                              ║
    echo ║  安装时请务必勾选 "Add Python to PATH" ！     ║
    echo ╚══════════════════════════════════════════════╝
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo    Python %PYVER%

:: 检查 Python 版本是否在支持范围内
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)
if %PYMAJOR% NEQ 3 (
    echo.
    echo    [警告] 需要 Python 3.x，当前版本 %PYVER% 不支持！
    pause
    exit /b 1
)
if %PYMINOR% LSS 10 (
    echo.
    echo    [警告] 需要 Python 3.10 或更高版本，当前版本 %PYVER%
    echo    请到 https://www.python.org/downloads/ 下载新版本
    pause
    exit /b 1
)
if %PYMINOR% GTR 14 (
    echo.
    echo    [警告] 当前 Python %PYVER% 版本较新，离线包可能不兼容
    echo    推荐使用 Python 3.10 ~ 3.13
    echo.
    choice /c YN /m "    是否继续安装？(Y=继续 / N=退出)"
    if errorlevel 2 exit /b 1
)

:: ────────────────────────────────────────
:: 2. 检查 venv 模块
:: ────────────────────────────────────────
echo.
echo [2/5] 检查虚拟环境模块...
python -c "import venv" >nul 2>&1
if errorlevel 1 (
    echo.
    echo    venv 模块不可用！
    echo.
    echo    这通常是因为 Python 从 Microsoft Store 安装的。
    echo    请按以下步骤修复：
    echo    1. 卸载 Microsoft Store 版本的 Python
    echo       （设置 - 应用 - 搜索 Python - 卸载）
    echo    2. 从官网重新下载安装：https://www.python.org/downloads/
    echo    3. 安装时选 Customize installation，确保所有组件都勾选
    echo    4. 重新运行本脚本
    echo.
    pause
    exit /b 1
)
echo    venv 模块正常

:: ────────────────────────────────────────
:: 3. 创建虚拟环境
:: ────────────────────────────────────────
echo.
echo [3/5] 创建虚拟环境 (.venv)...
set "NEED_VENV=1"
if exist ".venv\Scripts\activate.bat" (
    findstr /i /c:"%CD%" ".venv\Scripts\activate.bat" >nul 2>&1
    if not errorlevel 1 (
        set "NEED_VENV=0"
        echo    虚拟环境已存在且路径正确，跳过创建
    ) else (
        echo    虚拟环境路径不匹配，正在重建...
        rmdir /s /q .venv >nul 2>&1
    )
)
if "%NEED_VENV%"=="1" (
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo    创建虚拟环境失败！
        echo    请确保 Python 是从 python.org 官网安装的
        pause
        exit /b 1
    )
    echo    虚拟环境创建成功
)

:: ────────────────────────────────────────
:: 4. 从本地 vendor 目录离线安装所有依赖
:: ────────────────────────────────────────
echo.
echo [4/5] 从本地离线包安装依赖（无需联网）...

if not exist "vendor" (
    echo.
    echo    [错误] 未找到 vendor 目录！
    echo    请确保 vendor 文件夹与本脚本在同一目录下。
    echo    该文件夹包含所有预打包的依赖库。
    pause
    exit /b 1
)

.venv\Scripts\pip install --no-index --find-links vendor -r aion-chat\requirements.txt -q
if errorlevel 1 (
    echo.
    echo    离线安装出错，正在尝试逐个安装...
    echo.
    for %%p in (
        fastapi uvicorn httpx aiosqlite opencv-python
        Pillow sounddevice numpy webrtcvad-wheels pyncm
        pydantic python-multipart ebooklib beautifulsoup4
        lxml websockets pywin32 psutil akshare chinese-calendar
    ) do (
        echo    正在安装 %%p ...
        .venv\Scripts\pip install --no-index --find-links vendor %%p -q 2>nul
        if errorlevel 1 (
            echo    [跳过] %%p 安装失败，可能缺少对应 Python %PYVER% 的预编译包
        )
    )
)
echo    依赖安装完成

:: ────────────────────────────────────────
:: 5. 验证安装结果
:: ────────────────────────────────────────
echo.
echo [5/5] 检查安装结果...
set "ALL_OK=1"

.venv\Scripts\python -c "import fastapi; print('    FastAPI  ', fastapi.__version__)" 2>nul || (echo     [!] FastAPI 未安装 & set "ALL_OK=0")
.venv\Scripts\python -c "import cv2; print('    OpenCV   ', cv2.__version__)" 2>nul || (echo     [!] OpenCV 未安装 & set "ALL_OK=0")
.venv\Scripts\python -c "import numpy; print('    NumPy    ', numpy.__version__)" 2>nul || (echo     [!] NumPy 未安装 & set "ALL_OK=0")
.venv\Scripts\python -c "import pyncm; print('    PyNCM     OK')" 2>nul || (echo     [!] PyNCM 未安装 & set "ALL_OK=0")
.venv\Scripts\python -c "import psutil; print('    psutil   ', psutil.__version__)" 2>nul || (echo     [!] psutil 未安装 & set "ALL_OK=0")
.venv\Scripts\python -c "import ebooklib; print('    ebooklib  OK')" 2>nul || (echo     [!] ebooklib 未安装 & set "ALL_OK=0")
.venv\Scripts\python -c "import bs4; print('    BS4       OK')" 2>nul || (echo     [!] BeautifulSoup4 未安装 & set "ALL_OK=0")
.venv\Scripts\python -c "import lxml; print('    lxml      OK')" 2>nul || (echo     [!] lxml 未安装 & set "ALL_OK=0")
.venv\Scripts\python -c "import websockets; print('    websockets OK')" 2>nul || (echo     [!] websockets 未安装 & set "ALL_OK=0")
.venv\Scripts\python -c "import pandas; print('    pandas   ', pandas.__version__)" 2>nul || (echo     [!] pandas 未安装 & set "ALL_OK=0")

echo.
if "%ALL_OK%"=="1" (
    echo ════════════════════════════════════════
    echo   安装完成！所有依赖已就绪
    echo   现在可以双击「一键启动.bat」运行了
    echo ════════════════════════════════════════
) else (
    echo ════════════════════════════════════════
    echo   部分依赖安装失败（见上方 [!] 标记）
    echo   可能原因：Python 版本与预编译包不兼容
    echo   推荐使用 Python 3.10 ~ 3.13
    echo ════════════════════════════════════════
)
echo.
pause
