# Local project bindings of library skills; the backend of the "Projects" UI.
# Usage: projects.ps1 status
#        projects.ps1 register <folder> | unregister <folder>
#        projects.ps1 bind <folder> <skill>
#        projects.ps1 unbind <folder> <skill> [global|library]
#        projects.ps1 global <skill> on|off
# Exit codes: 3 the last binding needs a global-access choice (JSON on stdout);
#             4 the binding is saved but not applied (see status).
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$commands = @{status = @(0); register = @(1); unregister = @(1); bind = @(2); unbind = @(2, 3); global = @(2)}
$command = [string]@($args)[0]
$arguments = @($args | Select-Object -Skip 1)
if (-not $command -or -not $commands.ContainsKey($command) -or $commands[$command] -notcontains $arguments.Count) {
    [Console]::Error.WriteLine('Usage: projects.ps1 status|register|unregister|bind|unbind|global ...; see the header of projects.ps1.')
    exit 2
}

function Invoke-Git {
    param([string[]]$GitArgs, [string]$Directory = $repo)
    $output = & git -C $Directory @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "git failed (exit $LASTEXITCODE)." }
    return $output
}

$previousEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$lock = $null
$result = 1
try {
    . (Join-Path $repo 'scripts\client.ps1')
    . (Join-Path $repo 'scripts\bindings.ps1')
    if ($command -ne 'status') {
        # The background client uses the same lock; wait for it instead of skipping.
        $lockPath = Invoke-Git -GitArgs @('rev-parse', '--git-path', 'agent-skills-sync.lock')
        if (-not [IO.Path]::IsPathRooted($lockPath)) { $lockPath = Join-Path $repo $lockPath }
        $timeout = 300
        if ($env:AGENT_SKILLS_LOCK_TIMEOUT) { $timeout = [int]$env:AGENT_SKILLS_LOCK_TIMEOUT }
        $deadline = [DateTime]::UtcNow.AddSeconds($timeout)
        while (-not $lock) {
            try {
                $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            } catch [IO.IOException] {
                if ([DateTime]::UtcNow -gt $deadline) { throw 'Synchronization is still running; nothing changed. Retry later.' }
                Start-Sleep -Milliseconds 200
            }
        }
    }
    Read-Bindings
    # Commands return their exit code last; informational output goes to the host.
    $outputs = @(switch ($command) {
        'status' { Invoke-Status }
        'register' { Invoke-Register -Path $arguments[0] }
        'unregister' { Invoke-Unregister -Path $arguments[0] }
        'bind' { Invoke-Bind -Path $arguments[0] -Name $arguments[1] }
        'unbind' { Invoke-Unbind -Path $arguments[0] -Name $arguments[1] -Choice ([string]@($arguments)[2]) }
        'global' { Invoke-Global -Name $arguments[0] -Value $arguments[1] }
    })
    $result = $outputs[-1]
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $result = 1
} finally {
    [Console]::OutputEncoding = $previousEncoding
    if ($lock) { $lock.Dispose() }
}
exit ([int]$result)
