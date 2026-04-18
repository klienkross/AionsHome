@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   清理个人数据（打包给朋友前使用）
echo ========================================
echo.
echo   将删除以下内容:
echo     - 聊天数据库 (chat.db)
echo     - 导出的聊天记录 (chats/)
echo     - 监控日志 (monitor_logs/)
echo     - 摄像头截图 (screenshots/)
echo     - 上传的图片/视频 (uploads/)
echo     - 活动日志 (activity_logs/)
echo     - TTS 语音缓存 (tts_cache/)
echo     - 临时文件 (tmp/)
echo     - 聊天状态 + 记忆锚点
echo     - 定位配置 + 定位状态（含家坐标/高德Key）
echo     - 世界书人设 (重置为空白)
echo     - 小剧场角色预设 (theater_personas.json)
echo     - 奥罗斯幽林游戏数据 (ghost_forest/)
echo     - 阅读书籍数据 (books/)
echo     - API Key (需要朋友自己填)
echo     - 虚拟环境 (朋友需重新安装)
echo     - 源码中硬编码的 API Key (重置为空)
echo     - 火山引擎 TTS 配置 + 输出
echo     - 个人笔记/备份文件
echo     - .vscode 配置
echo.
echo   !! 请确认这是【复制出来的副本】!!
echo   !! 不要在你自己的原始文件夹里运行 !!
echo.
set /p CONFIRM=确认清理? 输入 Y 继续: 
if /i not "%CONFIRM%"=="Y" (
    echo 已取消。
    pause
    exit /b 0
)

echo.
echo 正在清理...

:: ── aion-chat/data/ ──
if exist "aion-chat\data\chat.db" del /q "aion-chat\data\chat.db"
if exist "aion-chat\data\chat_status.json" del /q "aion-chat\data\chat_status.json"
if exist "aion-chat\data\digest_anchor.json" del /q "aion-chat\data\digest_anchor.json"
if exist "aion-chat\data\cam_config.json" del /q "aion-chat\data\cam_config.json"
if exist "aion-chat\data\location_status.json" del /q "aion-chat\data\location_status.json"

if exist "aion-chat\data\chats" rmdir /s /q "aion-chat\data\chats"
mkdir "aion-chat\data\chats"

if exist "aion-chat\data\monitor_logs" rmdir /s /q "aion-chat\data\monitor_logs"
mkdir "aion-chat\data\monitor_logs"

if exist "aion-chat\data\screenshots" rmdir /s /q "aion-chat\data\screenshots"
mkdir "aion-chat\data\screenshots"

if exist "aion-chat\data\uploads" rmdir /s /q "aion-chat\data\uploads"
mkdir "aion-chat\data\uploads"

if exist "aion-chat\data\activity_logs" rmdir /s /q "aion-chat\data\activity_logs"
mkdir "aion-chat\data\activity_logs"

if exist "aion-chat\data\tts_cache" rmdir /s /q "aion-chat\data\tts_cache"
mkdir "aion-chat\data\tts_cache"

if exist "aion-chat\data\tmp" rmdir /s /q "aion-chat\data\tmp"
mkdir "aion-chat\data\tmp"

:: ── 清理奥罗斯幽林游戏数据 ──
if exist "aion-chat\data\ghost_forest" rmdir /s /q "aion-chat\data\ghost_forest"
mkdir "aion-chat\data\ghost_forest"

:: ── 清理阅读书籍数据 ──
if exist "aion-chat\data\books" rmdir /s /q "aion-chat\data\books"
mkdir "aion-chat\data\books"

:: ── 清理小剧场角色预设 ──
if exist "aion-chat\data\theater_personas.json" del /q "aion-chat\data\theater_personas.json"

:: 重置 settings.json（清空所有 API Key）
echo {} > "aion-chat\data\settings.json"

:: 重置世界书人设
echo {"ai_persona": "", "user_persona": "", "ai_name": "AI", "user_name": ""} > "aion-chat\data\worldbook.json"

:: 重置定位配置（清空高德Key和家坐标）
echo {"amap_key": "", "home_lng": 0, "home_lat": 0, "home_threshold": 500, "heartbeat_outdoor_min": 5, "heartbeat_home_min": 30, "poi_types": {"餐饮美食": "050000", "风景名胜": "110000", "休闲娱乐": "100000", "购物": "060000"}, "poi_radius": 2000, "enabled": false, "quiet_hours_enabled": true, "quiet_hours_start": "00:00", "quiet_hours_end": "10:00", "movement_threshold": 500} > "aion-chat\data\location_config.json"

:: ── 删除个人笔记/备份文件 ──
if exist "自己看的存档.txt" del /q "自己看的存档.txt"
if exist "MW_RAG_Backup_2026-04-04.json" del /q "MW_RAG_Backup_2026-04-04.json"
if exist "import_mw_rag.py" del /q "import_mw_rag.py"
if exist "fix_schedules.py" del /q "fix_schedules.py"

:: ── 删除 Active 独立监控截图 ──
if exist "Active\screenshots" rmdir /s /q "Active\screenshots"
mkdir "Active\screenshots"

:: ── 删除 .vscode 配置 ──
if exist ".vscode" rmdir /s /q ".vscode"

:: ── 清理 Android App 硬编码 IP ──
set "JAVA_DIR=AionApp\app\src\main\java\com\aion\chat"
if exist "%JAVA_DIR%\LauncherActivity.java" (
    powershell -Command "(Get-Content '%JAVA_DIR%\LauncherActivity.java' -Encoding UTF8) -replace 'http://[0-9.]+:8080/chat', 'http://192.168.xx.xxx:8080/chat' | Set-Content '%JAVA_DIR%\LauncherActivity.java' -Encoding UTF8"
)
if exist "%JAVA_DIR%\WebViewActivity.java" (
    powershell -Command "(Get-Content '%JAVA_DIR%\WebViewActivity.java' -Encoding UTF8) -replace 'http://[0-9.]+:8080/chat', 'http://192.168.xx.xxx:8080/chat' | Set-Content '%JAVA_DIR%\WebViewActivity.java' -Encoding UTF8"
)
if exist "%JAVA_DIR%\AionPushService.java" (
    powershell -Command "(Get-Content '%JAVA_DIR%\AionPushService.java' -Encoding UTF8) -replace 'http://[0-9.]+:8080/chat', 'http://192.168.xx.xxx:8080/chat' | Set-Content '%JAVA_DIR%\AionPushService.java' -Encoding UTF8"
)

:: ── 删除虚拟环境 ──
if exist ".venv" rmdir /s /q ".venv"

echo.
echo ========================================
echo   清理完成!
echo   朋友拿到后按顺序操作:
echo   1. 双击「一键安装环境.bat」
echo   2. 双击「一键启动.bat」
echo   3. 浏览器打开 localhost:8080
echo   4. 设置里填 API Key
echo ========================================
echo.
pause
