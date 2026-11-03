Option Explicit

' 静默启动微信知识库托盘（无黑框）
Dim shell, fso, root
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
shell.Run """" & root & "\venv\Scripts\pythonw.exe"" """ & root & "\tray_app.py"" --scroll-history --scroll-pages 5", 0, False
