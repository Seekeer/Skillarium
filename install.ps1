# Windows PowerShell 5.1+; installs junctions and a per-user hourly task.
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Sync', 'Link', 'Uninstall', 'Status', 'Updates', 'Review', 'Approve', 'Resolve')][string]$Mode = 'Install',
    [string]$Skill,
    [string]$Commit,
    [ValidateSet('keep-ours', 'take-upstream', 'merged')][string]$Strategy,
    [string]$Library
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Use install.sh on Linux.'
}
$engine = $PSScriptRoot
. (Join-Path $engine 'scripts\locate.ps1')
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = "AgentSkills-Hourly-$($identity.User.Value)"
if ($Mode -eq 'Uninstall') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Write-Output 'Hourly task removed. Skills and junctions were kept.'
    return
}
$repo = Resolve-Library -Explicit $Library -Engine $engine

function Invoke-Git {
    param([string[]]$GitArgs, [string]$Directory = $repo)
    $output = & git -C $Directory @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "git failed (exit $LASTEXITCODE)." }
    return $output
}

$lockPath = Invoke-Git -GitArgs @('rev-parse', '--git-path', 'agent-skills-sync.lock')
if (-not [IO.Path]::IsPathRooted($lockPath)) { $lockPath = Join-Path $repo $lockPath }
try {
    $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch [IO.IOException] {
    Write-Output 'Another sync is running or the lock is unavailable; skipped.'
    return
}
$failed = $false
$previousEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$previousEnv = @{}
foreach ($key in @('GIT_TERMINAL_PROMPT', 'GCM_INTERACTIVE', 'GIT_SSH_COMMAND')) {
    $previousEnv[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
try {
    # Prevent catalog Git from prompting in scheduled runs.
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'Never'
    if (-not $env:GIT_SSH_COMMAND) { $env:GIT_SSH_COMMAND = 'ssh -oBatchMode=yes -oConnectTimeout=15' }
    . (Join-Path $engine 'scripts\client.ps1')
    if ($Mode -in @('Review', 'Approve', 'Resolve') -and -not $Skill) { throw "-Skill is required for -Mode $Mode." }
    if ($Mode -in @('Approve', 'Resolve') -and -not $Commit) { throw "-Commit is required for -Mode $Mode." }
    if ($Mode -eq 'Resolve' -and -not $Strategy) { throw '-Strategy is required for -Mode Resolve.' }
    switch ($Mode) {
        'Status' { Show-Status; return }
        'Updates' {
            try { Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'fetch', '-q', '--prune', 'origin') | Out-Null }
            catch { Write-Warning 'Offline: showing known proposals.' }
            Show-Updates; return
        }
        'Review' { Show-Review -Name $Skill; return }
        'Approve' { Approve-Update -Name $Skill -Commit $Commit; return }
        'Resolve' { Resolve-Update -Name $Skill -Commit $Commit -Strategy $Strategy; return }
    }
    . (Join-Path $engine 'scripts\bindings.ps1')
    if ($Mode -ne 'Link') {
        try { Sync-Repository }
        catch { Write-Warning $_.Exception.Message; $failed = $true }
    }
    # Global links and registered project bindings follow the local choices.
    Read-Bindings
    if (-not (Sync-Bindings)) { throw 'Some skill links could not be applied; existing files were preserved.' }
    if ($failed) { throw 'Synchronization incomplete; existing files were preserved.' }
    if ($Mode -in @('Install', 'Sync')) {
        # Legacy layout (this engine inside the library): once Skillarium is installed, the task
        # runs its current engine instead, and the engine files can leave the library.
        $runner = $engine
        if ((Get-LongPath $engine) -eq $repo -and -not $env:SKILLARIUM_NO_BRIDGE) {
            . (Join-Path $engine 'scripts\bridge.ps1')
            $bridged = Install-SkillariumBridge -Library $repo
            if ($bridged) { $runner = $bridged }
        }
        $launcher = Join-Path $runner 'scripts\sync-hidden.vbs'
        $hostPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
        if (-not (Test-Path -LiteralPath $hostPath -PathType Leaf)) {
            throw 'Windows Script Host is required for background synchronization.'
        }
        # The engine runs the library given here; the legacy layout keeps its old arguments.
        $taskArguments = "//B //NoLogo `"$launcher`""
        if ((Get-LongPath $runner) -ne $repo) { $taskArguments += " `"$repo`"" }
        $action = New-ScheduledTaskAction -Execute $hostPath -Argument $taskArguments -WorkingDirectory $repo
    }
    if ($Mode -eq 'Install') {
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) -RepetitionInterval (New-TimeSpan -Hours 1)
        $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Synchronize personal agent skills hourly while logged in.' -Force | Out-Null
        Write-Output "Installed: $taskName"
    } elseif ($Mode -eq 'Sync') {
        # Upgrade existing installations without changing their schedule or identity.
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($task -and ($task.Actions.Count -ne 1 -or
            $task.Actions[0].Execute -ne $action.Execute -or
            $task.Actions[0].Arguments -ne $action.Arguments -or
            $task.Actions[0].WorkingDirectory -ne $action.WorkingDirectory)) {
            Set-ScheduledTask -TaskName $taskName -Action $action | Out-Null
        }
    }
} finally {
    [Console]::OutputEncoding = $previousEncoding
    foreach ($key in $previousEnv.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previousEnv[$key], 'Process')
    }
    $lock.Dispose()
}
