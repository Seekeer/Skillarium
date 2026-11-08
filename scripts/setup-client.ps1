# Connect this Windows machine to a skill library; run by `skillarium client`.
#
#   skillarium client -Library https://gitea.example.org/owner/skills.git [-Path <folder>] [-Name <name>] [-NoApp]
#
# Installs the Skillarium app of this engine's release, stores the Gitea token
# (prompted, or -TokenFile) in the Git credential helper after checking write
# access, clones the library (the first one into %USERPROFILE%\Skillarium\library,
# further ones into %USERPROFILE%\Skillarium\<name>; an existing clone is kept),
# adds it to the end of the connected libraries in the Skillarium configuration,
# links the skills and schedules the hourly sync of all libraries. Repeating it
# with a connected library's URL only repairs that connection. Keep this file ASCII.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Library,
    [string]$Path,
    # Short local name of the library; by default the repository name.
    [string]$Name,
    [string]$TokenFile,
    [string]$Machine,
    [switch]$NoApp,
    [switch]$NoSchedule,
    [switch]$NoLaunch,
    # Run by the app's first-run screen: the app is already installed.
    [switch]$FromApp
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$engine = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'locate.ps1')

function Invoke-Git {
    param([string[]]$GitArgs, [switch]$Quiet)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & git @GitArgs 2>&1; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0 -and -not $Quiet) { throw "git $($GitArgs[0]) failed: $(($output | Out-String).Trim())" }
    if ($code -ne 0) { return $null }
    return (($output | Where-Object { $_ -is [string] }) -join "`n").Trim()
}

function Split-LibraryUrl {
    param([string]$Url)
    if ($Url -notmatch '^(https?)://([^/@]+)((?:/[^/]+)*)/([^/]+)/([^/]+?)(?:\.git)?/?$') {
        throw 'Expected the library as https://host/owner/name.git (no credentials in the URL).'
    }
    return [pscustomobject]@{ Scheme = $Matches[1]; Host = $Matches[2]; Base = "$($Matches[1])://$($Matches[2])$($Matches[3])"
        Owner = $Matches[4]; Name = $Matches[5] }
}

function Read-Token {
    if ($TokenFile) { $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim() }
    else {
        Write-Output 'Gitea access token for the library (write:repository); it goes to the Git credential helper.'
        $secure = Read-Host -AsSecureString 'Token'
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    }
    if (-not $token) { throw 'No token given.' }
    return $token.Trim()
}

function Invoke-Gitea {
    param([string]$Api, [string]$Token, [string]$Resource)
    try {
        return Invoke-RestMethod -UseBasicParsing -Uri ($Api + $Resource) -Headers @{ Authorization = "token $Token"; Accept = 'application/json' }
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        throw "Gitea $Resource answered $(if ($status) { $status } else { $_.Exception.Message })."
    }
}

# Host, hosting prefix, owner and name, lower case: the same repository on another host differs.
function Get-LibraryKey {
    param([string]$Url)
    try { $parts = Split-LibraryUrl $Url } catch { return $null }
    return "$($parts.Base)/$($parts.Owner)/$($parts.Name)".ToLowerInvariant()
}

function Find-KnownLibrary {
    param([object[]]$Libraries, [string]$Key)
    foreach ($item in $Libraries) {
        $itemKey = Get-LibraryKey $item.url
        if (-not $itemKey -and (Test-Path -LiteralPath ([IO.Path]::Combine($item.path, '.git')))) {
            $itemKey = Get-LibraryKey (Invoke-Git -GitArgs @('-C', $item.path, 'config', '--get', 'remote.origin.url') -Quiet)
        }
        if ($itemKey -and $itemKey -eq $Key) { return $item }
    }
    return $null
}

# The configuration changes under the lock of the links (see bindings.ps1), so a parallel
# connection, disconnection or reordering is never lost.
function Enter-ConfigLock {
    $folder = Join-Path $env:LOCALAPPDATA 'agent-skills'
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(300)
    while ($true) {
        try { return [IO.File]::Open((Join-Path $folder 'bindings.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
        catch [IO.IOException] {
            if ([DateTime]::UtcNow -gt $deadline) { throw 'Skill links are being updated by another run; retry later.' }
            Start-Sleep -Milliseconds 200
        }
    }
}

# Token per host: another account on the same host would replace the stored one.
function Test-OtherAccount {
    param($Target, [string]$Login)
    $request = "protocol=$($Target.Scheme)`nhost=$($Target.Host)`n`n"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $env:GIT_TERMINAL_PROMPT = '0'
        $stored = @($request | & git -c credential.interactive=false credential fill 2>$null)
    } finally { $ErrorActionPreference = $previous; Remove-Item Env:GIT_TERMINAL_PROMPT -ErrorAction SilentlyContinue }
    $user = @($stored | Where-Object { $_ -like 'username=*' } | ForEach-Object { $_.Substring(9) }) | Select-Object -First 1
    return ($user -and $user -ne $Login)
}

function Save-Credential {
    param($Target, [string]$Login, [string]$Token)
    if (-not (Invoke-Git -GitArgs @('config', '--get', 'credential.helper') -Quiet)) {
        $helper = if (Invoke-Git -GitArgs @('credential-manager', '--version') -Quiet) { 'manager' } else { 'store' }
        Invoke-Git -GitArgs @('config', '--global', 'credential.helper', $helper) | Out-Null
        Write-Output "Git credential helper: $helper"
    }
    $request = "protocol=$($Target.Scheme)`nhost=$($Target.Host)`nusername=$Login`npassword=$Token`n`n"
    # A console input encoding with a BOM (Windows "UTF-8 for worldwide language support")
    # prefixes piped input with it, and Git then sees no protocol field.
    $inputEncoding = [Console]::InputEncoding
    try {
        [Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
        $request | & git credential approve
    } finally { [Console]::InputEncoding = $inputEncoding }
    if ($LASTEXITCODE -ne 0) { throw 'The Git credential helper did not store the token.' }
}

function Install-App {
    $release = Join-Path $engine 'release.json'
    $entry = $null
    if (Test-Path -LiteralPath $release) { $entry = (Get-Content -LiteralPath $release -Raw | ConvertFrom-Json).app.windows }
    if (-not $entry) { Write-Warning 'This Skillarium release has no Windows app; skills and sync work without it.'; return $null }
    $folder = Join-Path ([IO.Path]::GetTempPath()) ('skillarium-app-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    try {
        $installer = Join-Path $folder ([IO.Path]::GetFileName([string]$entry.file))
        Invoke-WebRequest -UseBasicParsing -Uri ([string]$entry.url) -OutFile $installer
        $digest = (Get-FileHash -LiteralPath $installer -Algorithm SHA512).Hash.ToLowerInvariant()
        if ((Get-Item -LiteralPath $installer).Length -ne [long]$entry.size -or $digest -ne [string]$entry.sha512) {
            throw "Checksum mismatch for $($entry.url); the app was not installed."
        }
        # NSIS per-user installation into %LOCALAPPDATA%\Programs\Skillarium.
        $process = Start-Process -FilePath $installer -ArgumentList '/S' -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "The app installer failed (exit $($process.ExitCode))." }
        Write-Output 'Skillarium app installed.'
        return (Join-Path $env:LOCALAPPDATA 'Programs\Skillarium\Skillarium.exe')
    } finally { [IO.Directory]::Delete($folder, $true) }
}

try {
    $target = Split-LibraryUrl $Library
    $url = "$($target.Base)/$($target.Owner)/$($target.Name).git"
    $api = "$($target.Base)/api/v1"
    # A library connected before keeps its folder and name: repeating only repairs it.
    $key = Get-LibraryKey $url
    $libraries = @(Get-ConfiguredLibraries)
    $known = Find-KnownLibrary $libraries $key
    if ($known) {
        if (-not $Path) { $Path = $known.path }
        if (-not $Name) { $Name = $known.name }
    }
    if (-not $Name) { $Name = $target.Name }
    if (-not $known -and @($libraries | Where-Object { $_.name -eq $Name }).Count) { $Name = "$($target.Owner)-$($target.Name)" }
    if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$') { throw "Invalid library name: $Name; pass -Name." }
    if (-not $known -and @($libraries | Where-Object { $_.name -eq $Name }).Count) { throw "A library named $Name is connected already; pass -Name." }
    if (-not $Path) {
        $leaf = if ($libraries.Count -and -not $known) { $Name } else { 'library' }
        $Path = Join-Path $env:USERPROFILE "Skillarium\$leaf"
    }
    $Path = [IO.Path]::GetFullPath($Path)
    $token = Read-Token
    $user = Invoke-Gitea $api $token '/user'
    $repository = Invoke-Gitea $api $token "/repos/$($target.Owner)/$($target.Name)"
    if (-not $repository.permissions.push) { throw "The token of $($user.login) cannot write to $($target.Owner)/$($target.Name)." }
    if (Test-OtherAccount $target $user.login) {
        Write-Warning "Git keeps one token per host: the stored token of another account on $($target.Host) is replaced; libraries of that account stop syncing."
    }
    Save-Credential $target $user.login $token
    Remove-Variable token

    if (Test-Path -LiteralPath (Join-Path $Path '.git')) {
        $origin = Invoke-Git -GitArgs @('-C', $Path, 'config', '--get', 'remote.origin.url') -Quiet
        $existing = $null
        try { $existing = Split-LibraryUrl $origin } catch { }
        if (-not $existing -or (Get-LibraryKey $origin) -ne $key) {
            throw "$Path is a clone of $origin, not of $url; pass -Path."
        }
        Write-Output "Existing clone kept: $Path"
    } elseif ((Test-Path -LiteralPath $Path) -and @(Get-ChildItem -LiteralPath $Path -Force).Count) {
        throw "$Path exists and is not a clone; pass -Path."
    } else {
        [IO.Directory]::CreateDirectory((Split-Path -Parent $Path)) | Out-Null
        Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'clone', '-q', $url, $Path) | Out-Null
        Write-Output "Library cloned into $Path"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Path 'agent-skills.json')) -or -not (Test-Path -LiteralPath (Join-Path $Path 'sources.json'))) {
        throw "$url is not a skill library."
    }
    if ($Machine) { Invoke-Git -GitArgs @('-C', $Path, 'config', 'agent-skills.machine', $Machine) | Out-Null }

    # Applied to the configuration as it is now, not as it was before the clone.
    $entry = [pscustomobject]@{name = $Name; path = $Path; url = $url}
    $configLock = Enter-ConfigLock
    try {
        $libraries = @(Get-ConfiguredLibraries)
        $current = Find-KnownLibrary $libraries $key
        if ($current) {
            $entry.name = $current.name
            $libraries = @($libraries | ForEach-Object { if ($_ -eq $current) { $entry } else { $_ } })
        } else {
            if (@($libraries | Where-Object { $_.name -eq $entry.name }).Count) { throw "A library named $($entry.name) is connected already; pass -Name." }
            $libraries = @($libraries | Where-Object { [IO.Path]::GetFullPath($_.path) -ne $Path }) + $entry
        }
        Write-SkillariumConfig (Read-SkillariumConfig) $libraries (Get-PreferredLibraries)
    } finally { $configLock.Dispose() }
    $Name = $entry.name

    $app = $null
    if (-not $NoApp -and -not $FromApp) { $app = Install-App }
    $mode = if ($NoSchedule) { 'Sync' } else { 'Install' }
    & (Join-Path $engine 'install.ps1') -Mode $mode -Library $Path
    if (-not $?) { throw 'Linking the skills failed; see the messages above.' }
    # Without the app nothing else updates the engine.
    if ($NoApp -and -not $FromApp -and -not $NoSchedule) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $engine 'scripts\skillarium.ps1') enable-updates
    }
    if ($app -and -not $NoLaunch -and (Test-Path -LiteralPath $app)) { Start-Process -FilePath $app }
    Write-Output "CONNECTED: $url in $Path as $Name ($($libraries.Count) libraries connected)"
} catch {
    # One plain line for the terminal and the app, not a PowerShell error record.
    [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
    exit 1
}
exit 0
