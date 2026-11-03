@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem 微信知识库采集托盘（无黑框常驻）
rem %~dp0 为本 bat 所在目录，整个项目移动后仍可用
rem ============================================================

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist "%ROOT%venv\Scripts\pythonw.exe" (
    echo [错误] 没找到虚拟环境：%ROOT%venv\Scripts\pythonw.exe
    pause
    exit /b 1
)

if not exist "%ROOT%tray_app.py" (
    echo [错误] 没找到 tray_app.py
    pause
    exit /b 1
)

start "" "%ROOT%venv\Scripts\pythonw.exe" "%ROOT%tray_app.py" --scroll-history --scroll-pages 5 %*
exit /b 0
