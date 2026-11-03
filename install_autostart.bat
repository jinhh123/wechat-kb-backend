@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem 安装开机自启：写用户环境变量 + 通用引导脚本
rem 不再把绝对路径写死在快捷方式里；项目移动后重跑本脚本即可
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LAUNCHER=%STARTUP%\WeChatKB.cmd"

if not exist "%ROOT%\autostart_launcher.cmd" (
    echo [错误] 没找到 autostart_launcher.cmd
    if /i not "%~1"=="--silent" pause
    exit /b 1
)

setx WECHAT_KB_HOME "%ROOT%" >nul
copy /y "%ROOT%\autostart_launcher.cmd" "%LAUNCHER%" >nul
if exist "%STARTUP%\WeChatKB.lnk" del /f /q "%STARTUP%\WeChatKB.lnk"

if exist "%LAUNCHER%" (
    echo [OK] 已安装开机自启：%LAUNCHER%
    echo [OK] WECHAT_KB_HOME=%ROOT%
) else (
    echo [失败] 引导脚本没有复制成功
)
if /i not "%~1"=="--silent" pause
endlocal
