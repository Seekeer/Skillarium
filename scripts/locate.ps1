# Library location and format for the engine's entry points (dot-sourced).
# The engine may live inside the library (legacy layout) or apart from it
# (installed Skillarium). Order: the -Library argument, SKILLARIUM_LIBRARY,
# the engine folder itself when it is a library, then the "library" field of
# the user's Skillarium configuration.

# Highest library format (agent-skills.json "format", absent = 1) this engine
# understands. A newer library is used read-only until Skillarium is updated.
$supportedFormat = 1

function Get-SkillariumConfigPath {
    $root = $env:APPDATA
    if (-not $root) { $root = [Environment]::GetFolderPath('ApplicationData') }
    return (Join-Path (Join-Path $root 'Skillarium') 'config.json')
}

function Test-LibraryFolder {
    param([string]$Path)
    # Path.Combine, not Join-Path: a library on a disconnected drive is just absent.
    return ($Path -and (Test-Path -LiteralPath ([IO.Path]::Combine($Path, 'sources.json')) -PathType Leaf) -and
        (Test-Path -LiteralPath ([IO.Path]::Combine($Path, 'agent-skills.json')) -PathType Leaf))
}

function Resolve-Library {
    param([string]$Explicit, [string]$Engine)
    $candidate = $Explicit
    if (-not $candidate) { $candidate = $env:SKILLARIUM_LIBRARY }
    if (-not $candidate -and (Test-LibraryFolder $Engine)) { $candidate = $Engine }
    if (-not $candidate) {
        $config = Get-SkillariumConfigPath
        if (Test-Path -LiteralPath $config -PathType Leaf) {
            $candidate = [string](Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json).library
        }
    }
    if (-not $candidate) {
        throw "Skill library not found: pass -Library <folder> or set ""library"" in $(Get-SkillariumConfigPath)"
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) { throw "Not a skill library: $candidate" }
    $full = Get-LongPath (Resolve-Path -LiteralPath $candidate).ProviderPath
    if (-not (Test-LibraryFolder $full)) { throw "Not a skill library: $candidate" }
    return $full
}

# Links are compared with their stored long targets, so 8.3 names (C:\Users\ABCDEF~1)
# are expanded; the file system lookup of a short name returns the long name.
function Get-LongPath {
    param([string]$Path)
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full -notlike '*~*') { return $full }
    $result = [IO.Path]::GetPathRoot($full)
    foreach ($part in $full.Substring($result.Length).Split('\')) {
        if (-not $part) { continue }
        $item = @(Get-ChildItem -LiteralPath $result -Filter $part -Force -ErrorAction SilentlyContinue)[0]
        if ($item) { $part = $item.Name }
        $result = Join-Path $result $part
    }
    return $result.TrimEnd('\')
}

# Library format of agent-skills.json at a revision (default: the working tree).
function Get-LibraryFormat {
    param([string]$Revision)
    if ($Revision) {
        # A missing revision (no origin yet) is format 1; git's error must not throw under Stop.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $text = (& git -C $repo show "${Revision}:agent-skills.json" 2>$null) -join "`n"; $code = $LASTEXITCODE }
        finally { $ErrorActionPreference = $previous }
        if ($code -ne 0) { return 1 }
    } else {
        $text = Get-Content -LiteralPath (Join-Path $repo 'agent-skills.json') -Raw -Encoding UTF8
    }
    $format = ($text | ConvertFrom-Json).format
    if ($null -eq $format) { return 1 }
    if ($format -isnot [int] -and $format -isnot [long] -or $format -lt 1) { throw "Invalid library format: $format" }
    return [int]$format
}

# Merges, commits and publications need the working tree, the shared branch
# and any given revision (a proposal) in a format this engine understands.
function Assert-LibraryWritable {
    param([string[]]$Revisions = @())
    Assert-LibraryFormat
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & git -C $repo rev-parse --verify -q '@{upstream}' 2>$null | Out-Null; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previous }
    if ($code -eq 0) { Assert-LibraryFormat '@{upstream}' }
    foreach ($revision in $Revisions) { Assert-LibraryFormat $revision }
}

# Throws a clear message when the library needs a newer Skillarium.
function Assert-LibraryFormat {
    param([string]$Revision)
    $format = Get-LibraryFormat $Revision
    if ($format -gt $supportedFormat) {
        throw "READ-ONLY: library format $format needs a newer Skillarium (this one supports $supportedFormat); skills keep working, edits stay local until you update."
    }
}
