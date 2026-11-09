# Library location and format for the engine's entry points (dot-sourced).
# The engine may live inside the library (legacy layout) or apart from it
# (installed Skillarium). Order: the -Library argument (a folder or the name of
# a connected library), SKILLARIUM_LIBRARY, the engine folder itself when it is
# a library, then the first of the libraries in the user's Skillarium
# configuration ("libraries", or the legacy single "library").

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

function Read-SkillariumConfig {
    $path = Get-SkillariumConfigPath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

# Writes the configuration; "library" keeps the first library for older engines and apps.
function Write-SkillariumConfig {
    param($Config, [object[]]$Libraries, [hashtable]$Prefer)
    $result = [ordered]@{}
    if ($Config) { foreach ($property in $Config.PSObject.Properties) { $result[$property.Name] = $property.Value } }
    $list = @($Libraries | ForEach-Object { [ordered]@{name = [string]$_.name; path = [string]$_.path; url = [string]$_.url} })
    $result['libraries'] = $list
    if ($list.Count) { $result['library'] = $list[0].path } else { $result.Remove('library') }
    if ($PSBoundParameters.ContainsKey('Prefer')) {
        $sorted = [ordered]@{}
        foreach ($key in @($Prefer.Keys | Sort-Object)) { $sorted[$key] = $Prefer[$key] }
        if ($sorted.Count) { $result['prefer'] = $sorted } else { $result.Remove('prefer') }
    }
    $path = Get-SkillariumConfigPath
    [IO.Directory]::CreateDirectory((Split-Path -Parent $path)) | Out-Null
    $json = ConvertTo-Json -InputObject $result -Depth 6
    [IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding($false)))
}

# owner/name of a library URL: the same for every colleague, unlike the local name.
function ConvertTo-RepoId {
    param([string]$Url)
    # A local remote (a folder) counts too: its last two parts.
    $text = ([string]$Url).Trim().Replace('\', '/').TrimEnd('/')
    if ($text -match '\.git$') { $text = $text.Substring(0, $text.Length - 4) }
    if ($text -match '[/:]([^/:]+)/([^/:]+)$') { return "$($Matches[1])/$($Matches[2])" }
    return $null
}

function Get-LibraryRepoId {
    param([string]$Path)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $url = [string](& git -C $Path config --get remote.origin.url 2>$null) } finally { $ErrorActionPreference = $previous }
    if (-not $url) { return $null }
    return (ConvertTo-RepoId $url)
}

# Connected libraries in priority order: [{name, path, url}] from "libraries", or the legacy "library".
function Get-ConfiguredLibraries {
    $config = Read-SkillariumConfig
    if (-not $config) { return @() }
    $result = @()
    if ($null -ne $config.libraries) {
        foreach ($item in @($config.libraries)) {
            if (-not $item.path) { continue }
            $result += [pscustomobject]@{name = [string]$item.name; path = [string]$item.path; url = [string]$item.url}
        }
    } elseif ($config.library) {
        $result += [pscustomobject]@{name = ''; path = [string]$config.library; url = ''}
    }
    foreach ($item in $result) {
        if ($item.name) { continue }
        $id = Get-LibraryRepoId $item.path
        $item.name = if ($id) { $id.Split('/')[1] } else { Split-Path -Leaf $item.path }
    }
    return $result
}

# True once a configuration names its libraries, even none: every library was disconnected.
function Test-LibrariesConfigured {
    $config = Read-SkillariumConfig
    return [bool]($config -and ($null -ne $config.libraries -or $config.library))
}

# Name for a library folder not yet in the list: its repository name, else the folder name.
function Get-NewLibraryName {
    param([string]$Path, [object[]]$Libraries)
    $id = Get-LibraryRepoId $Path
    $name = if ($id) { $id.Split('/')[1] } else { Split-Path -Leaf $Path }
    if (@($Libraries | Where-Object { $_.name -ceq $name }).Count) { $name = Split-Path -Leaf $Path }
    $base = $name
    $number = 2
    while (@($Libraries | Where-Object { $_.name -ceq $name }).Count) { $name = "$base-$number"; $number++ }
    return $name
}

# Skill name -> library name chosen on this machine over the order of the list.
function Get-PreferredLibraries {
    $result = @{}
    $config = Read-SkillariumConfig
    if ($config -and $config.prefer) {
        foreach ($property in $config.prefer.PSObject.Properties) { $result[$property.Name] = [string]$property.Value }
    }
    return $result
}

function Find-ConfiguredLibrary {
    param([string]$Name)
    foreach ($item in @(Get-ConfiguredLibraries)) { if ($item.name -ceq $Name) { return $item } }
    return $null
}

function Resolve-Library {
    param([string]$Explicit, [string]$Engine)
    $candidate = $Explicit
    if (-not $candidate) { $candidate = $env:SKILLARIUM_LIBRARY }
    # A connected library may also be given by its name.
    if ($candidate -and $candidate -notmatch '[\\/:]') {
        $named = Find-ConfiguredLibrary $candidate
        if ($named) { $candidate = $named.path }
    }
    if (-not $candidate -and (Test-LibraryFolder $Engine)) { $candidate = $Engine }
    if (-not $candidate) {
        # An unavailable library (a disconnected drive) must not block the others.
        $first = @(Get-ConfiguredLibraries | Where-Object { Test-LibraryFolder $_.path }) | Select-Object -First 1
        if (-not $first) { $first = @(Get-ConfiguredLibraries) | Select-Object -First 1 }
        if ($first) { $candidate = $first.path }
    }
    if (-not $candidate) {
        throw "Skill library not found: pass -Library <folder> or connect one with skillarium client ($(Get-SkillariumConfigPath))"
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) { throw "Not a skill library: $candidate" }
    $full = Get-LongPath (Resolve-Path -LiteralPath $candidate).ProviderPath
    if (-not (Test-LibraryFolder $full)) { throw "Not a skill library: $candidate" }
    return $full
}

# Every library whose skills reach this machine, in priority order: the connected ones
# and, when it is not one of them, the library this command was given ($Current, last).
function Get-LibraryCatalog {
    param([string]$Current)
    $comparison = [StringComparison]::Ordinal
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) { $comparison = [StringComparison]::OrdinalIgnoreCase }
    $result = @()
    foreach ($item in @(Get-ConfiguredLibraries)) {
        $path = $item.path
        $available = [bool](Test-LibraryFolder $path)
        $id = ConvertTo-RepoId $item.url
        if ($available) {
            $path = Get-LongPath (Resolve-Path -LiteralPath $path).ProviderPath
            $origin = Get-LibraryRepoId $path
            if ($origin) { $id = $origin }
        }
        $result += [pscustomobject]@{name = $item.name; path = $path; url = $item.url; repoId = $id; available = $available; configured = $true}
    }
    if ($Current -and -not @($result | Where-Object { [string]::Equals($_.path, $Current, $comparison) }).Count) {
        $id = Get-LibraryRepoId $Current
        $name = Get-NewLibraryName $Current $result
        $result += [pscustomobject]@{name = $name; path = $Current; url = ''; repoId = $id; available = $true; configured = $false}
    }
    return $result
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
