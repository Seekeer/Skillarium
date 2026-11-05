Option Explicit
Dim shell, files, engine, library, powershell, command, result
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
engine = files.GetParentFolderName(files.GetParentFolderName(WScript.ScriptFullName))
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
command = """" & powershell & """ -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & engine & "\install.ps1"" -Mode Sync"
' An installed engine receives its library as the argument; the legacy layout has none.
If WScript.Arguments.Count > 0 Then
    library = WScript.Arguments(0)
    command = command & " -Library """ & library & """"
    shell.CurrentDirectory = library
Else
    shell.CurrentDirectory = engine
End If
' Create the console hidden from the start, and preserve the result for Task Scheduler.
result = shell.Run(command, 0, True)
WScript.Quit result
