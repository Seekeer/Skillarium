# Windows PowerShell 5.1+; installs junctions and a per-user hourly task.
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Sync', 'Link', 'Uninstall', 'Status', 'Updates', 'Review', 'Approve', 'Resolve', 'Policy')][string]$Mode = 'Install',
    [string]$Skill,
    [string]$Commit,
    [ValidateSet('keep-ours', 'take-upstream', 'merged')][string]$Strategy,
    [ValidateSet('auto', 'confirm')][string]$Policy,
    [string]$Library
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Use install.sh on Linux.'
}
$engine = $PSScriptRoot
. (Join-Path $engine 'scripts\locate.ps1')
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
# SKILLARIUM_TASK_NAME keeps tests (and side-by-side installations) off the user's own task.
$taskName = if ($env:SKILLARIUM_TASK_NAME) { $env:SKILLARIUM_TASK_NAME } else { "AgentSkills-Hourly-$($identity.User.Value)" }
if ($Mode -eq 'Uninstall') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Write-Output 'Hourly task removed. Skills and junctions were kept.'
    return
}
# Every library was disconnected (an empty list, unlike a machine never connected).
if ($Mode -in @('Sync', 'Link') -and -not $Library -and -not $env:SKILLARIUM_LIBRARY -and
    -not (Test-LibraryFolder $engine) -and -not @(Get-ConfiguredLibraries).Count -and (Test-LibrariesConfigured)) {
    Write-Output 'No library is connected; nothing to sync.'
    return
}
$repo = Resolve-Library -Explicit $Library -Engine $engine
# Without an explicit library a synchronization covers every connected library.
$explicit = [bool]($Library -or $env:SKILLARIUM_LIBRARY -or (Test-LibraryFolder $engine))
# A disconnected library must not come back through a run that only started with it.
$commandLibrary = if ($explicit) { $repo } else { '' }

function Invoke-Git {
    param([string[]]$GitArgs, [string]$Directory = $repo)
    $output = & git -C $Directory @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "git failed (exit $LASTEXITCODE)." }
    return $output
}

# The lock of one library clone; $null while another run holds it.
function Open-LibraryLock {
    $path = Invoke-Git -GitArgs @('rev-parse', '--git-path', 'agent-skills-sync.lock')
    if (-not [IO.Path]::IsPathRooted($path)) { $path = Join-Path $repo $path }
    try { return [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch [IO.IOException] { return $null }
}

# Fetch and publish one library; its failure is recorded (and sets $failed) without
# stopping the others. Its messages stay on the output, so nothing is returned.
function Sync-LibraryClone {
    param([string]$Path)
    $repo = $Path
    $libraryLock = Open-LibraryLock
    if (-not $libraryLock) { Write-Output "Another sync of $Path is running; skipped."; $script:busy = $true; return }
    try {
        Sync-Repository
        Write-SyncStatus -Path $Path -Ok $true
    } catch {
        Write-Warning "${Path}: $($_.Exception.Message)"
        Write-SyncStatus -Path $Path -Ok $false -Message $_.Exception.Message
        $script:failed = $true
    } finally { $libraryLock.Dispose() }
}

$lock = $null
if ($Mode -notin @('Install', 'Sync', 'Link')) {
    $lock = Open-LibraryLock
    if (-not $lock) { Write-Output 'Another sync is running or the lock is unavailable; skipped.'; return }
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
    if ($Mode -in @('Review', 'Approve', 'Resolve', 'Policy') -and -not $Skill) { throw "-Skill is required for -Mode $Mode." }
    if ($Mode -in @('Approve', 'Resolve') -and -not $Commit) { throw "-Commit is required for -Mode $Mode." }
    if ($Mode -eq 'Resolve' -and -not $Strategy) { throw '-Strategy is required for -Mode Resolve.' }
    if ($Mode -eq 'Policy' -and -not $Policy) { throw '-Policy auto|confirm is required for -Mode Policy.' }
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
        'Policy' { Set-UpdatePolicy -Name $Skill -Policy $Policy; return }
    }
    . (Join-Path $engine 'scripts\bindings.ps1')
    if ($Mode -ne 'Link') {
        $targets = @($repo)
        if (-not $explicit) {
            $targets = @()
            foreach ($item in @(Get-ConfiguredLibraries)) {
                if (Test-LibraryFolder $item.path) { $targets += Get-LongPath $item.path }
                else { Write-Warning "Library $($item.name) is unavailable: $($item.path)"; $failed = $true }
            }
        }
        $busy = $false
        foreach ($target in $targets) {
            Sync-LibraryClone $target
        }
        # The one library given explicitly is being synced by another run, which also links.
        if ($explicit -and $busy) { return }
    }
    # Global links and registered project bindings of all libraries follow the local choices.
    $lock = Enter-BindingsLock
    # A library installed by hand joins the connected ones, so the hourly task needs no
    # argument and keeps syncing all of them. The legacy layout keeps its own arguments.
    $legacy = (Get-LongPath $engine) -eq $repo
    $configured = @(Get-ConfiguredLibraries)
    $known = @($configured | Where-Object { (Test-LibraryFolder $_.path) -and (Get-LongPath $_.path) -eq $repo }).Count
    if ($Mode -eq 'Install' -and $explicit -and -not $legacy -and -not $known) {
        $url = [string](Invoke-QuietGit -Directory $repo -GitArgs @('config', '--get', 'remote.origin.url'))
        $entry = [pscustomobject]@{name = (Get-NewLibraryName $repo $configured); path = $repo; url = $url}
        Write-SkillariumConfig (Read-SkillariumConfig) (@($configured) + $entry) (Get-PreferredLibraries)
        Write-Output "CONNECTED: $repo as $($entry.name)"
    }
    Read-Bindings
    if (-not (Sync-Bindings)) { throw 'Some skill links could not be applied; existing files were preserved.' }
    if (-not (Install-BuiltinSkills)) { throw 'Some built-in skills could not be linked; existing files were preserved.' }
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
        # Connected libraries come from the configuration; a library given only to this
        # command is passed on, and the legacy layout keeps its old arguments.
        $taskArguments = "//B //NoLogo `"$launcher`""
        $connected = @(Get-ConfiguredLibraries | Where-Object { (Test-LibraryFolder $_.path) -and (Get-LongPath $_.path) -eq $repo }).Count
        if ((Get-LongPath $runner) -ne $repo -and -not $connected) { $taskArguments += " `"$repo`"" }
        $action = New-ScheduledTaskAction -Execute $hostPath -Argument $taskArguments -WorkingDirectory $runner
    }
    if ($Mode -eq 'Install') {
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) -RepetitionInterval (New-TimeSpan -Hours 1)
        $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Synchronize personal agent skills hourly while logged in.' -Force | Out-Null
        Write-Output "Installed: $taskName"
    } elseif ($Mode -eq 'Sync' -and -not ((Get-LongPath $runner) -ne $repo -and -not $connected -and -not $legacy)) {
        # Upgrade existing installations without changing their schedule or identity; a sync
        # of a library given only to this command must not narrow the task to it.
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
    if ($lock) { $lock.Dispose() }
}
