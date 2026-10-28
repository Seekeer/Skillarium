Option Explicit
Dim shell, files, repo, powershell, command, result
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
repo = files.GetParentFolderName(files.GetParentFolderName(WScript.ScriptFullName))
shell.CurrentDirectory = repo
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
command = """" & powershell & """ -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & repo & "\install.ps1"" -Mode Sync"
' Create the console hidden from the start, and preserve the result for Task Scheduler.
result = shell.Run(command, 0, True)
WScript.Quit result
