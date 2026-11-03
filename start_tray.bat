@echo off
setlocal EnableExtensions
rem 静默启动 API + 托盘；真正的隐藏逻辑在 start_tray.vbs
rem 双击 bat 时只闪一下这个窗口，随后自动退出；推荐直接使用 start_tray.vbs / 桌面快捷方式
cd /d "%~dp0"
if not exist "%~dp0start_tray.vbs" (
    echo [错误] 没找到 start_tray.vbs
    pause
    exit /b 1
)
start "" wscript.exe "%~dp0start_tray.vbs"
exit /b 0
