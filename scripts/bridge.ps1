# Bridge of the legacy layout (the engine inside the library clone) to an
# installed Skillarium, dot-sourced by install.ps1. Once Skillarium is installed
# and configured with this clone, the hourly task runs current\scripts, and the
# engine files can leave the library. Failures only postpone the bridge.
# SKILLARIUM_NO_BRIDGE=1 switches it off. Keep this file ASCII.

# Returns the installed engine folder (...\current), or $null when it is not available yet.
function Install-SkillariumBridge {
    param([string]$Library)
    $root = if ($env:SKILLARIUM_HOME) { $env:SKILLARIUM_HOME } else { Join-Path $env:LOCALAPPDATA 'Skillarium' }
    $current = Join-Path $root 'current'
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $current 'VERSION'))) {
            $releases = if ($env:SKILLARIUM_RELEASES) { $env:SKILLARIUM_RELEASES } else { 'https://github.com/Seekeer/Skillarium/releases' }
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $script = Join-Path ([IO.Path]::GetTempPath()) ('skillarium-' + [Guid]::NewGuid().ToString('N') + '.ps1')
            try {
                Invoke-WebRequest -UseBasicParsing -Uri "$releases/latest/download/skillarium.ps1" -OutFile $script
                $savedLibrary = $env:SKILLARIUM_LIBRARY
                $env:SKILLARIUM_LIBRARY = $Library
                try { $output = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $script install 2>&1 }
                finally { $env:SKILLARIUM_LIBRARY = $savedLibrary }
                if ($LASTEXITCODE -ne 0) { throw (($output | Out-String).Trim()) }
                $output | ForEach-Object { Write-Host $_ }
            } finally { Remove-Item -LiteralPath $script -Force -ErrorAction SilentlyContinue }
        }
        $configPath = Join-Path $env:APPDATA 'Skillarium\config.json'
        $config = [ordered]@{}
        if (Test-Path -LiteralPath $configPath) {
            (Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties |
                ForEach-Object { $config[$_.Name] = $_.Value }
        }
        if (-not $config['library']) {
            $config['library'] = $Library
            [IO.Directory]::CreateDirectory((Split-Path -Parent $configPath)) | Out-Null
            [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        }
        return $current
    } catch {
        Write-Warning "Skillarium bridge postponed: $($_.Exception.Message) The next sync retries."
        return $null
    }
}
