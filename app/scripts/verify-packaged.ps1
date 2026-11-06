# Builds the Windows app the way a release does and runs it in a throwaway profile, so the
# real updater (GitHub Releases) and the engine deployment can be watched without touching
# the user's own installation, library, scheduled tasks or skill links.
#
#   powershell -File app\scripts\verify-packaged.ps1 [-Version 0.1.1] [-NoBuild]
#   powershell -File app\scripts\verify-packaged.ps1 -Stop
#
# -Version builds the app under another version number: with an older one the running app
# finds the newest release, downloads it and offers a restart. Do not press that restart:
# it would run the real installer for this user.
param(
    [string]$Version,
    # Lets a driver (Playwright connectOverCDP) click and take screenshots on loopback.
    [int]$DebugPort,
    [switch]$NoBuild,
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
$app = Split-Path $PSScriptRoot -Parent
$root = Split-Path $app -Parent
$work = Join-Path ([IO.Path]::GetTempPath()) 'skillarium-verify-packaged'
$pidFile = Join-Path $work 'app.pid'
$exe = Join-Path $app 'dist-app\win-unpacked\Skillarium.exe'

function Stop-VerifiedApp {
    if (-not (Test-Path -LiteralPath $pidFile)) { return }
    $id = [int](Get-Content -LiteralPath $pidFile)
    $process = Get-Process -Id $id -ErrorAction SilentlyContinue
    # Only the instance this script started: same id and the unpacked build's executable.
    if ($process -and $process.Path -and ([IO.Path]::GetFullPath($process.Path) -eq [IO.Path]::GetFullPath($exe))) {
        # Electron's helper processes keep files of the profile open until they exit too.
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $id" | ForEach-Object { $_.ProcessId })
        Stop-Process -Id $id
        foreach ($other in @($id) + $children) {
            $running = Get-Process -Id $other -ErrorAction SilentlyContinue
            if ($running) { $running.WaitForExit(10000) | Out-Null }
        }
        Write-Output "Stopped Skillarium (pid $id)."
    }
    Remove-Item -LiteralPath $pidFile
}

if ($Stop) { Stop-VerifiedApp; return }
Stop-VerifiedApp

if (-not $NoBuild) {
    # The engine keeps the version of package.json (build_engine.py checks it); -Version
    # changes only what the app reports to its updater.
    $packageVersion = (Get-Content -Raw -LiteralPath (Join-Path $app 'package.json') | ConvertFrom-Json).version
    & python (Join-Path $root 'release\build_engine.py') --version $packageVersion --stage (Join-Path $app 'build\engine')
    if ($LASTEXITCODE) { throw 'Staging the engine failed.' }
    Push-Location $app
    try {
        & npx vite build
        if ($LASTEXITCODE) { throw 'Renderer build failed.' }
        $arguments = @('electron-builder', '--win', 'nsis', '--publish', 'never')
        if ($Version) { $arguments += "-c.extraMetadata.version=$Version" }
        & npx @arguments
        if ($LASTEXITCODE) { throw 'Packaging failed.' }
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath $exe)) { throw "No packaged app at $exe; run without -NoBuild." }

# Deletes a folder without ever entering a link: a junction or symlink (the engine's
# current) is removed itself, never what it points to. Git keeps object files read-only.
function Remove-Tree {
    param([string]$Folder)
    foreach ($entry in [IO.Directory]::GetFileSystemEntries($Folder)) {
        $attributes = [IO.File]::GetAttributes($entry)
        $isFolder = [bool]($attributes -band [IO.FileAttributes]::Directory)
        if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
            if ($isFolder) { [IO.Directory]::Delete($entry) } else { [IO.File]::Delete($entry) }
        } elseif ($isFolder) {
            Remove-Tree $entry
        } else {
            [IO.File]::SetAttributes($entry, [IO.FileAttributes]::Normal)
            [IO.File]::Delete($entry)
        }
    }
    [IO.Directory]::Delete($Folder)
}

# A fresh profile and a minimal library for every run.
if (Test-Path -LiteralPath $work) {
    if ((Get-Item -LiteralPath $work -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "$work is a link; remove it by hand. This script deletes only a real folder it created."
    }
    Remove-Tree $work
}
$profileRoot = Join-Path $work 'profile'
$library = Join-Path $work 'library'
foreach ($folder in @("$profileRoot\AppData\Local", "$profileRoot\AppData\Roaming", $library)) {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
}
Copy-Item -LiteralPath (Join-Path $root 'templates\library\agent-skills.json') -Destination $library
'{"version": 2, "skills": {}}' | Set-Content -LiteralPath (Join-Path $library 'sources.json') -Encoding ASCII
$identity = @('-c', 'user.name=Verify', '-c', 'user.email=verify@localhost', '-c', 'core.autocrlf=false')
& git -C $library init -q
& git -C $library @identity add -A
& git -C $library @identity commit -q -m 'Verification library'

# The app and every engine call it makes inherit this environment.
$env:USERPROFILE = $profileRoot
$env:HOME = $profileRoot
$env:LOCALAPPDATA = "$profileRoot\AppData\Local"
$env:APPDATA = "$profileRoot\AppData\Roaming"
$env:SKILLARIUM_HOME = "$profileRoot\AppData\Local\Skillarium"
$env:SKILLARIUM_LIBRARY = $library
$env:SKILLARIUM_NO_BRIDGE = '1'
$env:SKILLARIUM_TASK_NAME = 'Skillarium-Verify-Packaged'
$env:SKILLARIUM_UPDATE_TASK = 'Skillarium-Verify-Packaged-Update'
# Electron's own profile is named explicitly too, not only through APPDATA.
$arguments = @("`"--user-data-dir=$profileRoot\AppData\Roaming\Skillarium`"")
if ($DebugPort) { $arguments += '--remote-debugging-address=127.0.0.1', "--remote-debugging-port=$DebugPort" }
$process = Start-Process -FilePath $exe -ArgumentList $arguments -PassThru
if ($DebugPort) { Write-Output "DevTools: http://127.0.0.1:$DebugPort" }
Set-Content -LiteralPath $pidFile -Value $process.Id
$builtVersion = (Get-Item -LiteralPath $exe).VersionInfo.ProductVersion
Write-Output "Skillarium $builtVersion (unpacked build) runs as pid $($process.Id)."
Write-Output "Profile: $profileRoot"
Write-Output "Library: $library"
Write-Output "Updater cache: $profileRoot\AppData\Local\skillarium-updater"
Write-Output "Stop: powershell -File `"$PSCommandPath`" -Stop"
