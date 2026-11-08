Option Explicit
' Hourly Skillarium self-update (skillarium enable-updates). Run through current\scripts, so
' each run takes the engine installed last. powershell.exe -WindowStyle Hidden still flashes
' its console; WScript.Shell.Run with window style 0 creates it hidden from the start.
Dim shell, files, engine, powershell, command, result
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
engine = files.GetParentFolderName(files.GetParentFolderName(WScript.ScriptFullName))
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
command = """" & powershell & """ -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & engine & "\scripts\skillarium.ps1"" self-update"
shell.CurrentDirectory = engine
' Preserve the result for Task Scheduler.
result = shell.Run(command, 0, True)
WScript.Quit result
