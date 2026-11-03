@echo off
chcp 65001 >nul
setlocal EnableExtensions

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if exist "%STARTUP%\WeChatKB.cmd" del /f /q "%STARTUP%\WeChatKB.cmd"
if exist "%STARTUP%\WeChatKB.lnk" del /f /q "%STARTUP%\WeChatKB.lnk"
echo [OK] 已移除开机自启
if /i not "%~1"=="--silent" pause
endlocal
