@echo off
rem 开机自启引导器：不写死项目路径，统一从 WECHAT_KB_HOME 读取
if not defined WECHAT_KB_HOME (
    echo [%DATE% %TIME%] WECHAT_KB_HOME 未设置 >> "%TEMP%\WeChatKB-autostart-error.log"
    exit /b 1
)
if not exist "%WECHAT_KB_HOME%\start_tray.vbs" (
    echo [%DATE% %TIME%] 找不到 %WECHAT_KB_HOME%\start_tray.vbs >> "%TEMP%\WeChatKB-autostart-error.log"
    exit /b 1
)
start "" wscript.exe "%WECHAT_KB_HOME%\start_tray.vbs"
