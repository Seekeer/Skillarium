# Native Windows integration test. Run only on a disposable Windows CI account.
# Uses actual junctions and Task Scheduler; never substitutes their implementations.
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windows is required.' }
$projectRoot = Split-Path -Parent $PSScriptRoot
# A unique task name keeps an existing installation of this account untouched.
$scheduledName = 'AgentSkills-Test-' + [Guid]::NewGuid().ToString('N')
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('agent skills windows ' + [Guid]::NewGuid().ToString('N'))
$testHome = Join-Path $testRoot 'isolated profile'
$seed = Join-Path $testRoot 'controller'
$remote = Join-Path $testRoot 'catalog.git'
$consumer = Join-Path $testRoot 'client with spaces'
$savedHome = $HOME
$savedEnvironment = @{}
foreach ($key in @('HOME', 'USERPROFILE', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM',
                   'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL')) {
    $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
$installedTask = $false

function Test-Assert {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Invoke-TestGit {
    param([string]$Directory, [string[]]$Arguments)
    $output = & git -C $Directory @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Git test operation failed: $($Arguments[0])" }
    return $output
}

try {
    [IO.Directory]::CreateDirectory($testHome) | Out-Null
    [IO.Directory]::CreateDirectory($seed) | Out-Null
    Set-Variable -Name HOME -Value $testHome -Scope Global -Force
    $env:HOME = $testHome
    $env:USERPROFILE = $testHome
    $env:GIT_CONFIG_GLOBAL = Join-Path $testRoot 'global.gitconfig'
    [IO.File]::WriteAllText($env:GIT_CONFIG_GLOBAL, '')
    $env:GIT_CONFIG_NOSYSTEM = '1'
    $env:GIT_AUTHOR_NAME = $env:GIT_COMMITTER_NAME = 'agent-skills-test'
    $env:GIT_AUTHOR_EMAIL = $env:GIT_COMMITTER_EMAIL = 'test@example.invalid'

    foreach ($relative in @('install.ps1', 'scripts/client.ps1', 'scripts/sync-hidden.vbs', '.gitattributes', 'agent-skills.json')) {
        $destination = Join-Path $seed $relative
        [IO.Directory]::CreateDirectory((Split-Path -Parent $destination)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $projectRoot $relative) -Destination $destination
    }
    $installerText = [IO.File]::ReadAllText((Join-Path $seed 'install.ps1'))
    $installerText = $installerText.Replace('"AgentSkills-Hourly-$($identity.User.Value)"', ('"' + $scheduledName + '"'))
    [IO.File]::WriteAllText((Join-Path $seed 'install.ps1'), $installerText, (New-Object Text.UTF8Encoding($false)))
    $payload = Join-Path $seed 'skills/windows-ci'
    [IO.Directory]::CreateDirectory($payload) | Out-Null
    [IO.File]::WriteAllText((Join-Path $payload 'SKILL.md'), "---`nname: windows-ci`ndescription: Native Windows test`n---`nInitial")
    [IO.File]::WriteAllBytes((Join-Path $payload 'helper.ps1'), [Text.Encoding]::UTF8.GetBytes("Write-Output test`r`n"))
    $registry = @{version = 2; skills = @{'windows-ci' = @{
        source = @{kind = 'git'; url = 'https://example.invalid/offline.git'; path = '.'; ref = 'refs/heads/main'}
        auto_update = $false; revision = ('a' * 40); content_sha256 = ('b' * 64)
    }}}
    [IO.File]::WriteAllText((Join-Path $seed 'sources.json'), ($registry | ConvertTo-Json -Depth 6))
    Invoke-TestGit -Directory $seed -Arguments @('init', '-b', 'main') | Out-Null
    Invoke-TestGit -Directory $seed -Arguments @('add', '.') | Out-Null
    Invoke-TestGit -Directory $seed -Arguments @('commit', '-m', 'Native Windows fixture') | Out-Null
    Invoke-TestGit -Directory $testRoot -Arguments @('init', '--bare', '--initial-branch=main', $remote) | Out-Null
    Invoke-TestGit -Directory $seed -Arguments @('remote', 'add', 'origin', $remote) | Out-Null
    Invoke-TestGit -Directory $seed -Arguments @('push', '-u', 'origin', 'main') | Out-Null
    Invoke-TestGit -Directory $testRoot -Arguments @('clone', $remote, $consumer) | Out-Null
    $installer = Join-Path $consumer 'install.ps1'

    # Link-only mode must exercise actual native junction creation.
    & $installer -Mode Link
    foreach ($catalog in @('.agents', '.claude')) {
        $linkPath = Join-Path $testHome "$catalog/skills/windows-ci"
        $link = Get-Item -LiteralPath $linkPath -Force
        Test-Assert ($link.LinkType -eq 'Junction') "Expected real junction: $linkPath"
        $target = [IO.Path]::GetFullPath([string]@($link.Target)[0]).TrimEnd('\')
        Test-Assert ($target -ieq (Join-Path $consumer 'skills/windows-ci')) 'Junction target differs.'
    }

    # Register a real task and inspect its definition, including quoted paths.
    $installedTask = $true
    & $installer -Mode Install
    $task = Get-ScheduledTask -TaskName $scheduledName
    $launcher = Join-Path $consumer 'scripts/sync-hidden.vbs'
    Test-Assert ($task.Actions[0].Execute -ieq (Join-Path $env:SystemRoot 'System32/wscript.exe')) 'Wrong scheduled executable.'
    Test-Assert ($task.Actions[0].Arguments.Contains(('"' + $launcher + '"'))) 'Launcher path is not quoted.'
    Test-Assert ($task.Actions[0].Arguments -match '//B //NoLogo') 'Task does not run in background.'
    Test-Assert ($task.Actions[0].WorkingDirectory -eq $consumer) 'Wrong task working directory.'
    Test-Assert ($task.Triggers[0].Repetition.Interval -eq 'PT1H') 'Task is not hourly.'
    Test-Assert ([string]$task.Principal.LogonType -eq 'Interactive') 'Unexpected task logon type.'
    Test-Assert ([string]$task.Settings.MultipleInstances -eq 'IgnoreNew') 'Overlapping runs are allowed.'
    # Do not start this task: a new login session would use the runner's real HOME.
    Disable-ScheduledTask -TaskName $scheduledName | Out-Null

    # Publication of local edits goes through pull requests: see tests/test_powershell.py.
    $installed = Join-Path $testHome '.agents/skills/windows-ci/SKILL.md'
    $helper = [IO.File]::ReadAllBytes((Join-Path $consumer 'skills/windows-ci/helper.ps1'))
    Test-Assert ([Text.Encoding]::UTF8.GetString($helper) -ceq "Write-Output test`r`n") 'Skill CRLF bytes changed.'

    [IO.File]::WriteAllText((Join-Path $seed 'skills/windows-ci/remote.txt'), 'Remote Windows update')
    Invoke-TestGit -Directory $seed -Arguments @('add', '.') | Out-Null
    Invoke-TestGit -Directory $seed -Arguments @('commit', '-m', 'Remote update') | Out-Null
    Invoke-TestGit -Directory $seed -Arguments @('push') | Out-Null
    & $installer -Mode Sync
    Test-Assert (Test-Path -LiteralPath (Join-Path $testHome '.claude/skills/windows-ci/remote.txt')) 'Remote update did not reach the junction.'

    # A foreign directory must survive a failed installation attempt.
    $foreign = Join-Path $testHome '.claude/skills/windows-ci'
    [IO.Directory]::Delete($foreign)
    [IO.Directory]::CreateDirectory($foreign) | Out-Null
    [IO.File]::WriteAllText((Join-Path $foreign 'private.txt'), 'keep')
    $failed = $false
    try { & $installer -Mode Link } catch { $failed = $true }
    Test-Assert $failed 'Foreign installation did not raise a conflict.'
    Test-Assert ([IO.File]::ReadAllText((Join-Path $foreign 'private.txt')) -eq 'keep') 'Foreign installation changed.'

    & $installer -Mode Uninstall
    $installedTask = $false
    Test-Assert (-not (Get-ScheduledTask -TaskName $scheduledName -ErrorAction SilentlyContinue)) 'Uninstall retained the task.'
    Test-Assert (Test-Path -LiteralPath $installed) 'Uninstall removed installed skills.'
    Write-Output 'VERIFIED: native Windows junctions, Git sync, task registration and uninstall.'
} finally {
    if ($installedTask) { Unregister-ScheduledTask -TaskName $scheduledName -Confirm:$false -ErrorAction Continue }
    # Remove junctions explicitly before recursive cleanup to protect targets.
    foreach ($catalog in @('.agents', '.claude')) {
        $linkPath = Join-Path $testHome "$catalog/skills/windows-ci"
        $link = Get-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue
        if ($link -and $link.LinkType -eq 'Junction') { [IO.Directory]::Delete($linkPath) }
    }
    Set-Variable -Name HOME -Value $savedHome -Scope Global -Force
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
    }
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
