# Windows PowerShell 5.1+; installs junctions and a per-user hourly task.
[CmdletBinding()]
param([ValidateSet('Install', 'Sync', 'Link', 'Uninstall')][string]$Mode = 'Install')
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Use install.sh on Linux.'
}
$repo = $PSScriptRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = "AgentSkills-Hourly-$($identity.User.Value)"
if ($Mode -eq 'Uninstall') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Write-Output 'Hourly task removed. Skills and junctions were kept.'
    return
}

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
    # Prevent both registry and source Git from prompting in scheduled runs.
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'Never'
    if (-not $env:GIT_SSH_COMMAND) { $env:GIT_SSH_COMMAND = 'ssh -oBatchMode=yes -oConnectTimeout=15' }
    if ($Mode -ne 'Link') {
        try {
            $remotes = @(Invoke-Git -GitArgs @('remote'))
            if ($remotes -notcontains 'origin') {
                Write-Warning 'No origin configured yet; linking local skills only.'
            } else {
                $changes = @(Invoke-Git -GitArgs @('status', '--porcelain', '--untracked-files=normal'))
                if ($changes.Count -gt 0) { throw 'Repository has local changes; Git update skipped.' }
                Invoke-Git -GitArgs @('symbolic-ref', '-q', 'HEAD') | Out-Null
                Invoke-Git -GitArgs @('rev-parse', '--verify', '@{upstream}') | Out-Null
                $pending = @(Invoke-Git -GitArgs @('rev-list', '@{upstream}..HEAD'))
                foreach ($revision in $pending) {
                    $message = (Invoke-Git -GitArgs @('show', '-s', '--format=%B', $revision)) -join "`n"
                    if ($message -notmatch '(?m)^Agent-Skills-Client: 1$') { throw 'Unpublished manual commits preserved.' }
                }
                if ($pending.Count) { Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'push') | Out-Null }
                Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'pull', '--ff-only')
            }
        } catch {
            Write-Warning $_.Exception.Message
            $failed = $true
        }
    }

    . (Join-Path $repo 'scripts\client.ps1')
    if ($Mode -ne 'Link' -and -not $failed) {
        try { Publish-LocalChanges }
        catch { Write-Warning $_.Exception.Message; $failed = $true }
    }
    Install-Registry
    if ($failed) { throw 'Synchronization incomplete; existing files were preserved.' }
    if ($Mode -eq 'Install') {
        $scriptPath = Join-Path $repo 'install.ps1'
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`" -Mode Sync" -WorkingDirectory $repo
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) -RepetitionInterval (New-TimeSpan -Hours 1)
        $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Synchronize personal agent skills hourly while logged in.' -Force | Out-Null
        Write-Output "Installed: $taskName"
    }
} finally {
    [Console]::OutputEncoding = $previousEncoding
    foreach ($key in $previousEnv.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previousEnv[$key], 'Process')
    }
    $lock.Dispose()
}
