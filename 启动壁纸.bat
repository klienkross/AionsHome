@echo off
chcp 65001 >nul
title Aion Wallpaper

echo ========================================
echo   Aion 动态壁纸 正在启动...
echo   关闭此窗口即关闭壁纸
echo ========================================

:: 查找 Chrome 路径
set "CHROME="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
) else (
    :: 尝试从注册表读取
    for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul') do set "CHROME=%%b"
)

if "%CHROME%"=="" (
    echo [错误] 未找到 Chrome 浏览器，请安装 Chrome 或手动修改此文件中的路径
    echo 你也可以直接在浏览器访问 http://localhost:8080/wallpaper 并按 F11 全屏
    pause
    exit /b
)

echo 正在使用 Chrome App 模式打开壁纸...
echo 提示：按 F11 可切换全屏，左右方向键切换壁纸
start "" "%CHROME%" --app=http://localhost:8080/wallpaper --start-fullscreen
