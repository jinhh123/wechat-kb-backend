@echo off
setlocal EnableExtensions
rem 已整合为“API + 托盘”统一静默启动
rem 不再直接运行 python.exe，避免弹出 FastAPI 黑框
cd /d "%~dp0"
if not exist "%~dp0start_tray.vbs" (
    echo [错误] 没找到 start_tray.vbs
    pause
    exit /b 1
)
start "" wscript.exe "%~dp0start_tray.vbs"
exit /b 0
