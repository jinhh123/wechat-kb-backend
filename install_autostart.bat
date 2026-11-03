@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem 安装开机自启：在当前用户 Startup 目录创建快捷方式
rem 删除用 uninstall_autostart.bat
rem ============================================================

set "ROOT=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\WeChatKB.lnk"

if not exist "%ROOT%start_tray.vbs" (
    echo [错误] 没找到 start_tray.vbs
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; " ^
  "$sc = $ws.CreateShortcut('%LNK%'); " ^
  "$sc.TargetPath = '%WINDIR%\System32\wscript.exe'; " ^
  "$q = [char]34; $sc.Arguments = $q + '%ROOT%start_tray.vbs' + $q; " ^
  "$sc.WorkingDirectory = '%ROOT%'; " ^
  "$sc.Description = '微信知识库采集托盘'; " ^
  "$sc.Save()"

if exist "%LNK%" (
    echo [OK] 已安装开机自启：%LNK%
) else (
    echo [失败] 快捷方式没有创建成功
)
pause
endlocal
