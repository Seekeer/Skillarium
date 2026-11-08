# Local project bindings of library skills; the backend of the "Projects" UI.
# Usage: projects.ps1 status
#        projects.ps1 register <folder> | unregister <folder>
#        projects.ps1 bind <folder> <skill> [<library>]
#        projects.ps1 unbind <folder> <skill> [global|library]
#        projects.ps1 global <skill> on|off
#        projects.ps1 conflicts | resolve <request.json>
#        projects.ps1 policy <skill> auto|confirm
#        projects.ps1 libraries | order <library>... | prefer <skill> <library>|auto
#        projects.ps1 disconnect <library> [--force]
# -Library picks the library of policy, conflicts and resolve (a folder or a connected
# library's name); links and bindings always cover every connected library.
# Exit codes: 3 the last binding needs a global-access choice (JSON on stdout);
#             4 the binding is saved but not applied (see status);
#             5 the synchronization conflict changed; nothing was merged.
$ErrorActionPreference = 'Stop'
$engine = $PSScriptRoot
. (Join-Path $engine 'scripts\locate.ps1')
$library = $null
$rest = @($args)
if ($rest.Count -ge 2 -and @('-Library', '--library') -contains [string]$rest[0]) {
    $library = [string]$rest[1]
    $rest = @($rest | Select-Object -Skip 2)
}
$commands = @{status = @(0); register = @(1); unregister = @(1); bind = @(2, 3); unbind = @(2, 3); global = @(2); conflicts = @(0); resolve = @(1); policy = @(2)
    libraries = @(0); order = @(1..20); prefer = @(2); disconnect = @(1, 2)}
$command = [string]@($rest)[0]
$arguments = @($rest | Select-Object -Skip 1)
if (-not $command -or -not $commands.ContainsKey($command) -or $commands[$command] -notcontains $arguments.Count) {
    [Console]::Error.WriteLine('Usage: projects.ps1 [-Library <folder|name>] status|register|unregister|bind|unbind|global|conflicts|resolve|policy|libraries|order|prefer|disconnect ...; see the header of projects.ps1.')
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
$bindingsLock = $null
$result = 1
try {
    # After the last library was disconnected, listings show nothing instead of failing.
    if ($command -cin @('status', 'libraries') -and -not $library -and -not $env:SKILLARIUM_LIBRARY -and
        -not (Test-LibraryFolder $engine) -and -not @(Get-ConfiguredLibraries).Count -and (Test-LibrariesConfigured)) {
        $repo = ''
    } else { $repo = Resolve-Library -Explicit $library -Engine $engine }
    $commandLibrary = ''
    if ($library -or $env:SKILLARIUM_LIBRARY -or (Test-LibraryFolder $engine)) { $commandLibrary = $repo }
    . (Join-Path $engine 'scripts\client.ps1')
    . (Join-Path $engine 'scripts\bindings.ps1')
    if ($command -cin @('resolve', 'policy')) {
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
    # Links and bindings span all libraries; their own lock comes after the library's.
    if ($command -cnotin @('status', 'conflicts', 'libraries')) { $bindingsLock = Enter-BindingsLock }
    Read-Bindings
    # Commands return their exit code last; informational output goes to the host.
    $outputs = @(switch ($command) {
        'status' { Invoke-Status }
        'register' { Invoke-Register -Path $arguments[0] }
        'unregister' { Invoke-Unregister -Path $arguments[0] }
        'bind' { Invoke-Bind -Path $arguments[0] -Name $arguments[1] -Library ([string]@($arguments)[2]) }
        'unbind' { Invoke-Unbind -Path $arguments[0] -Name $arguments[1] -Choice ([string]@($arguments)[2]) }
        'global' { Invoke-Global -Name $arguments[0] -Value $arguments[1] }
        'conflicts' { Invoke-Conflicts }
        'resolve' { Invoke-Resolve -RequestPath $arguments[0] }
        'policy' {
            if (@('auto', 'confirm') -cnotcontains $arguments[1]) { throw 'The policy is auto or confirm.' }
            Set-UpdatePolicy -Name $arguments[0] -Policy $arguments[1]
            0
        }
        'libraries' { Invoke-Libraries }
        'order' { Invoke-Order -Names $arguments }
        'prefer' { Invoke-Prefer -Name $arguments[0] -Library $arguments[1] }
        'disconnect' { Invoke-Disconnect -Name $arguments[0] -Flag ([string]@($arguments)[1]) }
    })
    $result = $outputs[-1]
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $result = 1
} finally {
    [Console]::OutputEncoding = $previousEncoding
    if ($bindingsLock) { $bindingsLock.Dispose() }
    if ($lock) { $lock.Dispose() }
}
exit ([int]$result)
