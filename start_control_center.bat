@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "%~dp0venv\Scripts\pythonw.exe" (
    echo [错误] 没找到 venv\Scripts\pythonw.exe
    pause
    exit /b 1
)
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0control_center.py"
endlocal
