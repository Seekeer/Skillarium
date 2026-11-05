# Connect this Windows machine to a skill library; run by `skillarium client`.
#
#   skillarium client -Library https://gitea.example.org/owner/skills.git [-Path <folder>] [-NoApp]
#
# Installs the Skillarium app of this engine's release, stores the Gitea token
# (prompted, or -TokenFile) in the Git credential helper after checking write
# access, clones the library (default %USERPROFILE%\Skillarium\library; an
# existing clone is kept), writes the Skillarium configuration, links the
# skills and schedules the hourly sync. Safe to repeat. Keep this file ASCII.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Library,
    [string]$Path = (Join-Path $env:USERPROFILE 'Skillarium\library'),
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

function Save-Credential {
    param($Target, [string]$Login, [string]$Token)
    if (-not (Invoke-Git -GitArgs @('config', '--get', 'credential.helper') -Quiet)) {
        $helper = if (Invoke-Git -GitArgs @('credential-manager', '--version') -Quiet) { 'manager' } else { 'store' }
        Invoke-Git -GitArgs @('config', '--global', 'credential.helper', $helper) | Out-Null
        Write-Output "Git credential helper: $helper"
    }
    $request = "protocol=$($Target.Scheme)`nhost=$($Target.Host)`nusername=$Login`npassword=$Token`n`n"
    $request | & git credential approve
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
    $Path = [IO.Path]::GetFullPath($Path)
    $token = Read-Token
    $user = Invoke-Gitea $api $token '/user'
    $repository = Invoke-Gitea $api $token "/repos/$($target.Owner)/$($target.Name)"
    if (-not $repository.permissions.push) { throw "The token of $($user.login) cannot write to $($target.Owner)/$($target.Name)." }
    Save-Credential $target $user.login $token
    Remove-Variable token

    if (Test-Path -LiteralPath (Join-Path $Path '.git')) {
        $origin = Invoke-Git -GitArgs @('-C', $Path, 'config', '--get', 'remote.origin.url') -Quiet
        $existing = $null
        try { $existing = Split-LibraryUrl $origin } catch { }
        if (-not $existing -or $existing.Owner -ne $target.Owner -or $existing.Name -ne $target.Name) {
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

    $configPath = Join-Path $env:APPDATA 'Skillarium\config.json'
    $config = [ordered]@{}
    if (Test-Path -LiteralPath $configPath) {
        (Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $config[$_.Name] = $_.Value }
    }
    $config['library'] = $Path
    [IO.Directory]::CreateDirectory((Split-Path -Parent $configPath)) | Out-Null
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))

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
    Write-Output "CONNECTED: $url in $Path"
} catch {
    # One plain line for the terminal and the app, not a PowerShell error record.
    [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
    exit 1
}
exit 0
