@echo off
chcp 65001 >nul
setlocal EnableExtensions

set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WeChatKB.lnk"

if exist "%LNK%" (
    del /f /q "%LNK%"
    echo [OK] 已移除开机自启
) else (
    echo [提示] 没有找到开机自启快捷方式
)
pause
endlocal
