@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem 抓取「群聊的聊天记录」窗口（截图 + Windows OCR）
rem 用法：先打开记录窗口，再双击本脚本
rem        也可传参：start_record_capture.bat --pages 1 --dry-run
rem %~dp0 为本 bat 所在目录，移动项目后仍可用
rem ============================================================

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist "%ROOT%venv\Scripts\python.exe" (
    echo [错误] 没找到虚拟环境：%ROOT%venv\Scripts\python.exe
    pause
    exit /b 1
)

if not exist "%ROOT%record_capture.py" (
    echo [错误] 没找到 record_capture.py
    pause
    exit /b 1
)

echo ============================================================
echo   聊天记录 OCR 抓取
echo   项目目录：%ROOT%
echo   请保持「群聊的聊天记录」窗口打开且不要最小化
echo ============================================================
echo.

"%ROOT%venv\Scripts\python.exe" "%ROOT%record_capture.py" %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [退出码 %EXITCODE%] 1=没找到窗口  2=置前失败  3=没识别到文字
)
pause
endlocal & exit /b %EXITCODE%
