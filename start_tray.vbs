Option Explicit

' 静默启动本地 API + 托盘（无黑框）
' 实际启动逻辑在 start_services.py：
'   - API 已在 8000 端口则跳过
'   - 托盘单实例互斥，重复执行安全
Dim shell, fso, root
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root

If Not fso.FileExists(root & "\venv\Scripts\pythonw.exe") Then
    shell.Run "cmd /c echo [%DATE% %TIME%] 找不到 venv\Scripts\pythonw.exe >> ""%TEMP%\WeChatKB-autostart-error.log""", 0, True
    WScript.Quit 1
End If

If Not fso.FileExists(root & "\start_services.py") Then
    shell.Run "cmd /c echo [%DATE% %TIME%] 找不到 start_services.py >> ""%TEMP%\WeChatKB-autostart-error.log""", 0, True
    WScript.Quit 1
End If

' pythonw.exe 无控制台；start_services.py 再以隐藏窗口启动 API/托盘
shell.Run """" & root & "\venv\Scripts\pythonw.exe"" """ & root & "\start_services.py""", 0, False
