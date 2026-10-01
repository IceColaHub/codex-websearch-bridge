' Codex web search bridge launcher (hidden, no console window)
' 自动按脚本自身所在目录去找 run-bridge.cmd，换个位置也能跑
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "cmd.exe /c """ & scriptDir & "\run-bridge.cmd""", 0, False
