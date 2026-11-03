@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem 本地知识库 API 启动脚本（FastAPI + uvicorn）
rem %~dp0 表示本 bat 所在目录，项目整体移动后依然可用。
rem ============================================================

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist "%ROOT%venv\Scripts\python.exe" (
    echo [错误] 没找到虚拟环境：%ROOT%venv\Scripts\python.exe
    echo        请先在本目录创建 venv，并安装 requirements.txt 里的依赖。
    pause
    exit /b 1
)

if not exist "%ROOT%api_server.py" (
    echo [错误] 没找到 api_server.py，请确认 bat 与脚本在同一目录。
    pause
    exit /b 1
)

echo ============================================================
echo   知识库 API
echo   项目目录：%ROOT%
echo   接口地址：http://127.0.0.1:8000/api/knowledge
echo   按 Ctrl+C 停止
echo ============================================================
echo.

"%ROOT%venv\Scripts\python.exe" "%ROOT%api_server.py" %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [退出码 %EXITCODE%] 请检查上面的错误信息。
)
pause
endlocal & exit /b %EXITCODE%
