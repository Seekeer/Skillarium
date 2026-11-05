# Isolated integration check: no real skills, credentials, or user task.
$ErrorActionPreference = 'Stop'
# Legacy-layout syncs would install Skillarium from GitHub; tests/test_bridge.py covers it.
$env:SKILLARIUM_NO_BRIDGE = '1'
$root = Split-Path $PSScriptRoot -Parent
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('agent skills ' + [Guid]::NewGuid().ToString('N'))
$taskName = 'AgentSkills-Test-' + [Guid]::NewGuid().ToString('N')
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
function Assert-True($Condition, $Message) {
    if (-not $Condition) { throw $Message }
}
function Invoke-Installer($Mode) {
    & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $fixture 'install.ps1') -Mode $Mode
    Assert-True ($LASTEXITCODE -eq 0) "Installer failed in $Mode mode."
}
$savedEnvironment = @{}
foreach ($key in @('HOME', 'USERPROFILE', 'LOCALAPPDATA')) {
    $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
try {
    $env:HOME = $env:USERPROFILE = Join-Path $fixture 'isolated profile'
    $env:LOCALAPPDATA = Join-Path $env:USERPROFILE 'AppData/Local'
    New-Item -ItemType Directory -Path (Join-Path $fixture 'scripts') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $root 'scripts\sync-hidden.vbs') -Destination (Join-Path $fixture 'scripts')
    # The child measures its own console visibility, including paths with spaces.
    $probe = @'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class ConsoleProbe {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
}
"@
Set-Content -LiteralPath (Join-Path $PSScriptRoot 'visible.txt') -Value ([ConsoleProbe]::IsWindowVisible([ConsoleProbe]::GetConsoleWindow()))
exit EXIT_CODE
'@
    foreach ($code in @(0, 7)) {
        $probe.Replace('EXIT_CODE', [string]$code) | Set-Content -LiteralPath (Join-Path $fixture 'install.ps1') -Encoding UTF8
        $process = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\wscript.exe') -ArgumentList ('//B //NoLogo "' + (Join-Path $fixture 'scripts\sync-hidden.vbs') + '"') -Wait -PassThru -WindowStyle Hidden
        Assert-True ($process.ExitCode -eq $code) "Launcher lost exit code $code."
        Assert-True ((Get-Content -LiteralPath (Join-Path $fixture 'visible.txt')) -eq 'False') 'Child console was visible.'
        Remove-Item -LiteralPath (Join-Path $fixture 'visible.txt')
    }
    Write-Output 'PASS: hidden console and exit codes 0/7.'
    $installer = Get-Content -LiteralPath (Join-Path $root 'install.ps1') -Raw
    $installer.Replace('"AgentSkills-Hourly-$($identity.User.Value)"', ('"' + $taskName + '"')) | Set-Content -LiteralPath (Join-Path $fixture 'install.ps1') -Encoding UTF8
    Copy-Item -LiteralPath (Join-Path $root 'scripts\client.ps1') -Destination (Join-Path $fixture 'scripts')
    Copy-Item -LiteralPath (Join-Path $root 'scripts\bindings.ps1') -Destination (Join-Path $fixture 'scripts')
    '{"version":2,"skills":{}}' | Set-Content -LiteralPath (Join-Path $fixture 'sources.json') -Encoding ASCII
    & git -C $fixture init -q
    Assert-True ($LASTEXITCODE -eq 0) 'Cannot initialize disposable repository.'
    Invoke-Installer Sync
    Assert-True (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) 'Sync recreated an absent task.'
    Invoke-Installer Install
    $task = Get-ScheduledTask -TaskName $taskName
    Assert-True ($task.Actions.Execute -like '*\wscript.exe') 'Install did not register the GUI host.'
    $before = [xml](Export-ScheduledTask -TaskName $taskName)
    $legacy = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile' -WorkingDirectory $fixture
    Set-ScheduledTask -TaskName $taskName -Action $legacy | Out-Null
    Invoke-Installer Sync
    $after = [xml](Export-ScheduledTask -TaskName $taskName)
    foreach ($section in @('Triggers', 'Principals', 'Settings', 'Actions')) {
        Assert-True ($before.Task.$section.OuterXml -eq $after.Task.$section.OuterXml) "Migration changed $section unexpectedly."
    }
    Start-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 250
        $task = Get-ScheduledTask -TaskName $taskName
        $info = $task | Get-ScheduledTaskInfo
    } while (($task.State -eq 'Running' -or $info.LastRunTime.Year -lt 2020) -and (Get-Date) -lt $deadline)
    Assert-True ($task.State -ne 'Running' -and $info.LastTaskResult -eq 0 -and $info.LastRunTime.Year -ge 2020) 'Scheduled sync failed or timed out.'
    Invoke-Installer Install
    Assert-True ((Get-ScheduledTask -TaskName $taskName).Actions.Execute -like '*\wscript.exe') 'Reinstall reverted hidden action.'
    Invoke-Installer Uninstall
    Assert-True (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) 'Uninstall kept the task.'
    Write-Output 'PASS: install, migration preserving schedule/identity/settings, scheduled sync, reinstall, uninstall.'
} finally {
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
    }
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $resolved = [IO.Path]::GetFullPath($fixture)
    if ($resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $resolved -Leaf) -like 'agent skills *') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
