# Skillarium engine manager and bootstrap for Windows (Windows PowerShell 5.1).
#
#   & ([scriptblock]::Create((irm https://github.com/Seekeer/Skillarium/releases/latest/download/skillarium.ps1))) client -Library <url>
#   skillarium status | self-update [-Channel stable|beta] | rollback | install [-Version <v>]
#
# The engine of each release lives in %LOCALAPPDATA%\Skillarium\versions\<version>\;
# current is a junction that schedules and the skillarium command always go through.
# Two previous versions are kept for rollback. Keep this file ASCII.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$releases = if ($env:SKILLARIUM_RELEASES) { $env:SKILLARIUM_RELEASES } else { 'https://github.com/Seekeer/Skillarium/releases' }
$api = if ($env:SKILLARIUM_API) { $env:SKILLARIUM_API } else { 'https://api.github.com/repos/Seekeer/Skillarium' }
$root = if ($env:SKILLARIUM_HOME) { $env:SKILLARIUM_HOME } else { Join-Path $env:LOCALAPPDATA 'Skillarium' }
$keep = 2
$versionPattern = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$'

function Show-Usage {
    @'
Usage: skillarium <command>
  status                         engine version, channel, library format and mode
  self-update [-Channel C]       install the newest release of the channel (stable|beta)
  rollback                       return to the previous version and skip the current one
  install [-Version V] [-Channel C]
                                 install a release (first installation or a fixed version)
  enable-updates | disable-updates
                                 hourly self-update task (machines without the app)
  client ...                     connect this machine to a library (skillarium client -Help)
  updates                        pending updates of external skills and conflicts
  review <skill>                 what an update changes (diff, versions, conflict copy)
  approve <skill> <commit>       accept exactly the reviewed result
  resolve <skill> <commit> keep-ours|take-upstream|merged
                                 settle a conflict
  add <folder|link> [--policy auto|confirm]
                                 add a skill: an own folder, or a link to a repository,
                                 a skill folder or its SKILL.md (skillarium add --help)
'@
}

function Compare-SkillariumVersion {
    param([string]$A, [string]$B)
    $ap = ''; $bp = ''
    if ($A.Contains('-')) { $ap = $A.Substring($A.IndexOf('-') + 1); $A = $A.Substring(0, $A.IndexOf('-')) }
    if ($B.Contains('-')) { $bp = $B.Substring($B.IndexOf('-') + 1); $B = $B.Substring(0, $B.IndexOf('-')) }
    $x = $A.Split('.'); $y = $B.Split('.')
    for ($i = 0; $i -lt 3; $i++) {
        $left = [long]$x[$i]; $right = [long]$y[$i]
        if ($left -ne $right) { if ($left -lt $right) { return -1 } else { return 1 } }
    }
    if ($ap -ceq $bp) { return 0 }
    if (-not $ap) { return 1 }
    if (-not $bp) { return -1 }
    $x = $ap.Split('.'); $y = $bp.Split('.')
    for ($i = 0; $i -lt [Math]::Max($x.Count, $y.Count); $i++) {
        if ($i -ge $x.Count) { return -1 }
        if ($i -ge $y.Count) { return 1 }
        if ($x[$i] -ceq $y[$i]) { continue }
        $xn = $x[$i] -match '^[0-9]+$'; $yn = $y[$i] -match '^[0-9]+$'
        if ($xn -and $yn) { if ([long]$x[$i] -lt [long]$y[$i]) { return -1 } else { return 1 } }
        if ($xn) { return -1 }
        if ($yn) { return 1 }
        if ([string]::CompareOrdinal($x[$i], $y[$i]) -lt 0) { return -1 } else { return 1 }
    }
    return 0
}

function Get-CurrentVersion {
    $file = Join-Path $root 'current\VERSION'
    if (Test-Path -LiteralPath $file) { return (Get-Content -LiteralPath $file -Raw).Trim() }
    return $null
}

function Get-Channel {
    $file = Join-Path $root 'channel'
    if (Test-Path -LiteralPath $file) {
        $value = (Get-Content -LiteralPath $file -Raw).Trim()
        if ($value -in @('stable', 'beta')) { return $value }
    }
    return 'stable'
}

function Write-Text {
    param([string]$Path, [string]$Text)
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Get-ConfigPath { Join-Path $env:APPDATA 'Skillarium\config.json' }

function Get-LibraryPath {
    if ($env:SKILLARIUM_LIBRARY) { return $env:SKILLARIUM_LIBRARY }
    $config = Get-ConfigPath
    if (Test-Path -LiteralPath $config) {
        $value = (Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json).library
        if ($value) { return [string]$value }
    }
    return $null
}

# Highest format of the library working tree and its shared branch; $null without a library.
function Get-LibraryFormat {
    param([string]$Library)
    if (-not $Library -or -not (Test-Path -LiteralPath (Join-Path $Library 'agent-skills.json'))) { return $null }
    $texts = @(Get-Content -LiteralPath (Join-Path $Library 'agent-skills.json') -Raw -Encoding UTF8)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $shared = (& git -C $Library show '@{upstream}:agent-skills.json' 2>$null) -join "`n"
        if ($LASTEXITCODE -eq 0) { $texts += $shared }
    } finally { $ErrorActionPreference = $previous }
    $best = $null
    foreach ($text in $texts) {
        try { $value = ($text | ConvertFrom-Json).format } catch { continue }
        if ($null -eq $value) { $value = 1 }
        if (($value -is [int] -or $value -is [long]) -and $value -ge 1 -and ($null -eq $best -or $value -gt $best)) { $best = [int]$value }
    }
    return $best
}

function Enter-UpdateLock {
    [IO.Directory]::CreateDirectory((Join-Path $root 'versions')) | Out-Null
    try {
        $script:lockStream = [IO.File]::Open((Join-Path $root '.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch { throw 'Another Skillarium update is running.' }
}

function Read-Feed {
    param([string]$Channel, [string]$Version)
    if ($Version) {
        if ($Version -notmatch $versionPattern) { throw "Invalid version: $Version" }
        $url = "$releases/download/v$Version/latest.json"
    } elseif ($Channel -eq 'beta') {
        $items = Invoke-RestMethod -UseBasicParsing -Uri "$api/releases?per_page=20" -Headers @{ Accept = 'application/vnd.github+json' }
        $release = @($items | Where-Object { -not $_.draft }) | Select-Object -First 1
        if (-not $release) { throw 'No Skillarium releases published.' }
        $url = "$releases/download/$($release.tag_name)/latest.json"
    } else {
        $url = "$releases/latest/download/latest.json"
    }
    # Release assets come as application/octet-stream: Content is then a byte array.
    $content = (Invoke-WebRequest -UseBasicParsing -Uri $url).Content
    $document = if ($content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($content) } else { [string]$content }
    $feed = $document | ConvertFrom-Json
    $entry = $feed.engine.windows
    if ([string]$feed.version -notmatch $versionPattern -or -not $entry -or
        [string]$entry.sha512 -notmatch '^[0-9a-f]{128}$' -or $null -eq $feed.format.max) {
        throw "Invalid release feed: $url"
    }
    $min = if ($null -ne $feed.format.min) { [int]$feed.format.min } else { 1 }
    return [pscustomobject]@{ Version = [string]$feed.version; Min = $min; Max = [int]$feed.format.max
        Url = [string]$entry.url; Sha512 = [string]$entry.sha512; Size = [long]$entry.size; Document = $document }
}

function Test-Compatible {
    param($Feed)
    $format = Get-LibraryFormat (Get-LibraryPath)
    if ($null -eq $format) { return $true }
    if ($format -gt $Feed.Max) {
        Write-Warning "Skillarium $($Feed.Version) supports library format up to $($Feed.Max); the library has $format. Skipped."
        return $false
    }
    if ($format -lt $Feed.Min) {
        Write-Warning "Skillarium $($Feed.Version) needs library format $($Feed.Min) or newer; the library has $format. Skipped."
        return $false
    }
    return $true
}

function Get-Release {
    param($Feed)
    $target = Join-Path $root "versions\$($Feed.Version)"
    if (Test-Path -LiteralPath (Join-Path $target 'VERSION')) { return }
    $incoming = Join-Path $root ('versions\.incoming-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($incoming) | Out-Null
    try {
        $archive = Join-Path $incoming 'engine.zip'
        Invoke-WebRequest -UseBasicParsing -Uri $Feed.Url -OutFile $archive
        $size = (Get-Item -LiteralPath $archive).Length
        $digest = (Get-FileHash -LiteralPath $archive -Algorithm SHA512).Hash.ToLowerInvariant()
        if ($size -ne $Feed.Size -or $digest -ne $Feed.Sha512) { throw "Checksum mismatch for $($Feed.Url); nothing changed." }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $engine = Join-Path $incoming 'engine'
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $engine)
        $unpacked = Join-Path $engine 'VERSION'
        if (-not (Test-Path -LiteralPath $unpacked) -or (Get-Content -LiteralPath $unpacked -Raw).Trim() -ne $Feed.Version) {
            throw "Broken archive: $($Feed.Url)"
        }
        # The feed stays with the version, like on Linux.
        Write-Text (Join-Path $engine 'release.json') $Feed.Document
        if (Test-Path -LiteralPath $target) { [IO.Directory]::Delete($target, $true) }
        [IO.Directory]::Move($engine, $target)
    } finally {
        if (Test-Path -LiteralPath $incoming) { [IO.Directory]::Delete($incoming, $true) }
    }
}

# Removes a junction itself, never the folder it points to.
function Remove-Junction {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) { [IO.Directory]::Delete($Path, $false) }
}

function Set-Current {
    param([string]$Version)
    $next = Join-Path $root 'current.new'
    $current = Join-Path $root 'current'
    Remove-Junction $next
    # mklink, unlike New-Item in Windows PowerShell 5.1, accepts 8.3 names in the path.
    $output = & cmd.exe /d /c mklink /J "$next" "$(Join-Path $root "versions\$Version")" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Cannot create $next`: $output" }
    Remove-Junction $current
    [IO.Directory]::Move($next, $current)
    $history = Join-Path $root 'history'
    $lines = @()
    if (Test-Path -LiteralPath $history) { $lines = @(Get-Content -LiteralPath $history | Where-Object { $_ -and $_ -ne $Version }) }
    Write-Text $history ((@($lines) + $Version) -join "`n")
}

function Get-History {
    $history = Join-Path $root 'history'
    if (-not (Test-Path -LiteralPath $history)) { return @() }
    $lines = @(Get-Content -LiteralPath $history | Where-Object { $_ })
    [array]::Reverse($lines)
    return $lines
}

function Remove-OldVersions {
    $current = Get-CurrentVersion
    $kept = @(Get-History | Where-Object { $_ -ne $current } | Select-Object -First $keep)
    foreach ($folder in Get-ChildItem -LiteralPath (Join-Path $root 'versions') -Directory) {
        if ($folder.Name -eq $current -or $kept -contains $folder.Name -or $folder.Name.StartsWith('.')) { continue }
        [IO.Directory]::Delete($folder.FullName, $true)
    }
    $history = Join-Path $root 'history'
    $remaining = @(Get-History | Where-Object { $_ -eq $current -or $kept -contains $_ })
    [array]::Reverse($remaining)
    Write-Text $history ($remaining -join "`n")
}

function Install-Command {
    $bin = Join-Path $root 'bin'
    [IO.Directory]::CreateDirectory($bin) | Out-Null
    $script = Join-Path $root 'current\scripts\skillarium.ps1'
    $text = "@echo off`r`nrem Managed by Skillarium`r`nset `"SKILLARIUM_HOME=$root`"`r`n" +
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`" %*`r`n"
    [IO.File]::WriteAllText((Join-Path $bin 'skillarium.cmd'), $text, [Text.Encoding]::ASCII)
    # A custom SKILLARIUM_HOME (tests, portable setups) leaves the user PATH alone.
    if ($env:SKILLARIUM_HOME) { return }
    # The raw registry value keeps %VARIABLE% references; the .NET API would expand them.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $path = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $parts = @(if ($path) { $path.Split(';') | Where-Object { $_ } })
        if ($parts -contains $bin) { return }
        $key.SetValue('Path', ((@($parts) + $bin) -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
    } finally { $key.Close() }
    # Tell Explorer, so terminals it starts see the new PATH.
    if (-not ('Skillarium.Environment' -as [type])) {
        Add-Type -Namespace Skillarium -Name Environment -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);
'@
    }
    $result = [UIntPtr]::Zero
    [Skillarium.Environment]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result) | Out-Null
    Write-Output "Added $bin to the user PATH; new terminals find skillarium."
}

$updateTask = if ($env:SKILLARIUM_UPDATE_TASK) { $env:SKILLARIUM_UPDATE_TASK } else {
    'Skillarium-Update-' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }

# Hourly self-update for machines without the app (the app updates itself and its engine).
function Enable-Updates {
    $script = Join-Path $root 'current\scripts\skillarium.ps1'
    $arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" self-update"
    $action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(30) -RepetitionInterval (New-TimeSpan -Hours 1)
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
    Register-ScheduledTask -TaskName $updateTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Update the Skillarium engine hourly.' -Force | Out-Null
    Write-Output "Hourly Skillarium self-update enabled: $updateTask"
}

function Disable-Updates {
    if (Get-ScheduledTask -TaskName $updateTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $updateTask -Confirm:$false
    }
    Write-Output 'Skillarium self-update disabled.'
}

function Read-Options {
    param([object[]]$Arguments, [string[]]$Names)
    $options = @{}
    for ($i = 0; $i -lt $Arguments.Count; $i++) {
        $name = ([string]$Arguments[$i]).TrimStart('-')
        $match = $Names | Where-Object { $_ -eq $name }
        if (-not $match -or $i + 1 -ge $Arguments.Count) { throw "Unknown or incomplete option: $($Arguments[$i])" }
        $options[$match] = [string]$Arguments[++$i]
    }
    return $options
}

function Invoke-Install {
    param([object[]]$Arguments)
    $options = Read-Options $Arguments @('Version', 'Channel')
    $channel = if ($options.Channel) { $options.Channel } else { Get-Channel }
    if ($channel -notin @('stable', 'beta')) { throw "Unknown channel: $channel" }
    Enter-UpdateLock
    $feed = Read-Feed -Channel $channel -Version $options.Version
    if (-not (Test-Compatible $feed)) { throw "Skillarium $($feed.Version) cannot read this library; nothing installed." }
    Get-Release $feed
    Set-Current $feed.Version
    Write-Text (Join-Path $root 'channel') $channel
    Remove-Item -LiteralPath (Join-Path $root 'skip') -Force -ErrorAction SilentlyContinue
    Install-Command
    Remove-OldVersions
    Write-Output "INSTALLED: Skillarium $($feed.Version) in $(Join-Path $root 'current')"
}

function Invoke-SelfUpdate {
    param([object[]]$Arguments)
    $options = Read-Options $Arguments @('Channel')
    $channel = if ($options.Channel) { $options.Channel } else { Get-Channel }
    if ($channel -notin @('stable', 'beta')) { throw "Unknown channel: $channel" }
    Enter-UpdateLock
    Write-Text (Join-Path $root 'channel') $channel
    $current = Get-CurrentVersion
    $feed = Read-Feed -Channel $channel
    if ($current -and (Compare-SkillariumVersion $feed.Version $current) -le 0) {
        Write-Output "Up to date: Skillarium $current ($channel)"; return
    }
    $skip = Join-Path $root 'skip'
    if ((Test-Path -LiteralPath $skip) -and (Get-Content -LiteralPath $skip -Raw).Trim() -eq $feed.Version) {
        Write-Warning "Skillarium $($feed.Version) was rolled back; skipped until a newer release."; return
    }
    if (-not (Test-Compatible $feed)) { return }
    Get-Release $feed
    Set-Current $feed.Version
    Remove-OldVersions
    Write-Output "UPDATED: Skillarium $(if ($current) { $current } else { 'none' }) -> $($feed.Version)"
}

function Invoke-Rollback {
    Enter-UpdateLock
    $current = Get-CurrentVersion
    if (-not $current) { throw 'Skillarium is not installed.' }
    $skipFile = Join-Path $root 'skip'
    $skipped = if (Test-Path -LiteralPath $skipFile) { (Get-Content -LiteralPath $skipFile -Raw).Trim() } else { '' }
    # A version rolled back before is not a target either.
    $previous = Get-History | Where-Object { $_ -ne $current -and $_ -ne $skipped -and (Test-Path -LiteralPath (Join-Path $root "versions\$_\VERSION")) } |
        Select-Object -First 1
    if (-not $previous) { throw 'No previous version to roll back to.' }
    Write-Text (Join-Path $root 'skip') $current
    Set-Current $previous
    Write-Output "ROLLED BACK: Skillarium $current -> $previous; $current is skipped until a newer release"
}

function Show-SkillariumStatus {
    $current = Get-CurrentVersion
    Write-Output "Skillarium: $(if ($current) { $current } else { 'not installed' }) ($(Get-Channel)) in $root"
    $versions = Join-Path $root 'versions'
    if (Test-Path -LiteralPath $versions) {
        Write-Output "Installed versions: $((Get-ChildItem -LiteralPath $versions -Directory | Where-Object { -not $_.Name.StartsWith('.') } | ForEach-Object Name) -join ' ')"
    }
    $skip = Join-Path $root 'skip'
    if (Test-Path -LiteralPath $skip) { Write-Output "Skipped after rollback: $((Get-Content -LiteralPath $skip -Raw).Trim())" }
    $library = Get-LibraryPath
    if (-not $library) { Write-Output 'Library: not configured'; return }
    $format = Get-LibraryFormat $library
    $supported = $null
    $locate = Join-Path $root 'current\scripts\locate.ps1'
    if (Test-Path -LiteralPath $locate) {
        $line = Select-String -LiteralPath $locate -Pattern '^\$supportedFormat = (\d+)' | Select-Object -First 1
        if ($line) { $supported = [int]$line.Matches[0].Groups[1].Value }
    }
    $mode = if ($null -ne $format -and $null -ne $supported -and $format -gt $supported) { 'read-only (update Skillarium)' } else { 'normal' }
    Write-Output "Library: $library; format $(if ($null -ne $format) { $format } else { 'unknown' }), engine supports $(if ($supported) { $supported } else { 'unknown' }); mode: $mode"
}

$command = if ($args.Count) { [string]$args[0] } else { 'help' }
$rest = @(if ($args.Count -gt 1) { $args[1..($args.Count - 1)] })
$code = 0
try {
    switch ($command) {
        'status' { Show-SkillariumStatus }
        'self-update' { Invoke-SelfUpdate $rest }
        'rollback' { Invoke-Rollback }
        'install' { Invoke-Install $rest }
        'enable-updates' { Enable-Updates }
        'disable-updates' { Disable-Updates }
        'client' {
            if (-not (Get-CurrentVersion)) { Invoke-Install @() }
            if ($script:lockStream) { $script:lockStream.Dispose(); $script:lockStream = $null }
            & (Join-Path $root 'current\scripts\setup-client.ps1') @rest
            $code = $LASTEXITCODE
        }
        { $_ -in @('updates', 'review', 'approve', 'resolve') } {
            # The library's update review lives in the installer; the library comes from the configuration.
            if (-not (Get-CurrentVersion)) { throw 'Skillarium is not installed: skillarium install.' }
            $positional = @{ updates = @(); review = @('Skill'); approve = @('Skill', 'Commit'); resolve = @('Skill', 'Commit', 'Strategy') }[$command]
            if ($rest.Count -ne $positional.Count) {
                throw "Usage: skillarium $command$(-join ($positional | ForEach-Object { ' <' + $_.ToLower() + '>' }))"
            }
            if ($command -eq 'resolve' -and $rest[2] -notin @('keep-ours', 'take-upstream', 'merged')) {
                throw 'The strategy is keep-ours, take-upstream or merged.'
            }
            $installerArgs = @('-Mode', ($command.Substring(0, 1).ToUpper() + $command.Substring(1)))
            for ($i = 0; $i -lt $positional.Count; $i++) { $installerArgs += @(('-' + $positional[$i]), [string]$rest[$i]) }
            if ($script:lockStream) { $script:lockStream.Dispose(); $script:lockStream = $null }
            & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -NoProfile -NonInteractive -ExecutionPolicy Bypass `
                -File (Join-Path $root 'current\install.ps1') @installerArgs
            $code = $LASTEXITCODE
        }
        'add' {
            # py first: on a machine without Python, python3/python are Store stubs.
            $python = $null
            foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
                $found = Get-Command $candidate[0] -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($found -and $found.Source -notlike '*\WindowsApps\*') { $python = @($found.Source) + @($candidate | Select-Object -Skip 1); break }
            }
            if (-not $python) { throw 'skillarium add needs Python 3 (python.org or winget install Python.Python.3.12).' }
            if (-not (Get-CurrentVersion)) { Invoke-Install @() }
            if ($script:lockStream) { $script:lockStream.Dispose(); $script:lockStream = $null }
            $pythonArgs = @($python | Select-Object -Skip 1)
            & $python[0] @pythonArgs (Join-Path $root 'current\server\manage.py') add @rest
            $code = $LASTEXITCODE
        }
        { $_ -in @('help', '--help', '-h', '-Help') } { Show-Usage }
        default { Show-Usage; $code = 2 }
    }
} catch {
    # One plain line, not a wrapped error record.
    [Console]::Error.WriteLine("skillarium: $($_.Exception.Message)")
    if ($env:SKILLARIUM_DEBUG) { [Console]::Error.WriteLine($_.ScriptStackTrace) }
    $code = 1
} finally {
    if ($script:lockStream) { $script:lockStream.Dispose() }
}
# Run from a file: an exit code. Run from memory (irm | scriptblock): exit would close the
# user's PowerShell window, so the code is left in $LASTEXITCODE instead.
if ($PSCommandPath) { exit $code }
$global:LASTEXITCODE = $code
