# Local skill availability: global links and project bindings of every connected
# library. Project lists live in each project's Git; registered paths, global
# choices and link ownership stay in local state that is never published. One
# state covers all libraries; a skill name in several of them is linked from the
# first library in the configured order unless another one is preferred for it.
$bindingsFile = Join-Path $cache 'bindings.json'
$projectList = '.agent-skills.json'
$agentFolders = @('.agents', '.claude')
$utf8 = New-Object Text.UTF8Encoding($false)
# The library given to this command explicitly joins the catalog even when it is not
# connected; entry points set it ('' when the command was given none).
if ($null -eq $commandLibrary) { $commandLibrary = $repo }
$pathComparison = [StringComparison]::Ordinal
if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) { $pathComparison = [StringComparison]::OrdinalIgnoreCase }

# JSON for the UI goes straight to stdout so command results stay plain exit codes.
function Write-Json {
    param($Value)
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject $Value -Depth 10 -Compress))
}

function Test-SamePath {
    param([string]$Left, [string]$Right)
    return [string]::Equals($Left, $Right, $pathComparison)
}

function ConvertTo-Plain {
    param($Value)
    if ($Value -is [Management.Automation.PSCustomObject]) {
        $table = @{}
        foreach ($property in $Value.PSObject.Properties) { $table[$property.Name] = ConvertTo-Plain $property.Value }
        return $table
    }
    if ($Value -is [Array]) { return ,@($Value | ForEach-Object { ConvertTo-Plain $_ }) }
    return $Value
}

function Write-TextFile {
    param([string]$Path, [string]$Text)
    $temporary = "$Path.$PID.tmp"
    [IO.File]::WriteAllText($temporary, $Text, $utf8)
    try {
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } catch {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
        throw
    }
}

# A path in the temporary folder that no longer exists: what a test run leaves behind.
function Test-DisposableLibrary {
    param([string]$Path)
    if (-not $Path -or (Test-Path -LiteralPath $Path)) { return $false }
    $full = [IO.Path]::GetFullPath($Path)
    $roots = @([IO.Path]::GetTempPath(), $env:TEMP, $env:TMP,
        (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Temp'))
    foreach ($root in $roots) {
        if (-not $root) { continue }
        $prefix = [IO.Path]::GetFullPath($root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        if ($full.StartsWith($prefix, $pathComparison)) { return $true }
    }
    return $false
}

# Skills of every library reaching this machine. A name present in several libraries goes
# to the first of them in the configured order, unless a library is preferred for it.
function Initialize-Catalog {
    param([string]$Current = $commandLibrary)
    $script:catalog = @(Get-LibraryCatalog -Current $Current)
    $script:copies = @{}
    $script:winners = @{}
    $script:preferred = Get-PreferredLibraries
    foreach ($library in $script:catalog) {
        $library | Add-Member -NotePropertyName skills -NotePropertyValue @() -Force
        if (-not $library.available) { continue }
        try { $library.skills = @(Get-NamesOf $library.path) }
        catch {
            # An unreadable registry must not unlink the library's skills: treat it as offline.
            Write-Warning "$($library.name): $($_.Exception.Message)"
            $library.available = $false
            continue
        }
        foreach ($name in $library.skills) {
            if (-not $script:copies.ContainsKey($name)) { $script:copies[$name] = @() }
            $script:copies[$name] += $library
        }
    }
    foreach ($name in @($script:copies.Keys)) {
        $winner = $script:copies[$name][0]
        if ($script:preferred.ContainsKey($name)) {
            $chosen = @($script:copies[$name] | Where-Object { $_.name -ceq $script:preferred[$name] })
            if ($chosen.Count) { $winner = $chosen[0] }
        }
        $script:winners[$name] = $winner
    }
}

function Get-NamesOf {
    param([string]$Path)
    $repo = $Path
    return @(Get-LibraryNames)
}

function Find-CatalogLibrary {
    param([string]$Name)
    foreach ($library in $script:catalog) { if ($library.name -ceq $Name) { return $library } }
    return $null
}

# The library whose skills folder holds a link target; $null outside every library.
function Find-TargetLibrary {
    param([string]$Target)
    foreach ($library in $script:catalog) {
        $prefix = [IO.Path]::GetFullPath([IO.Path]::Combine($library.path, 'skills')).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        if ($Target -and $Target.StartsWith($prefix, $pathComparison)) { return $library }
    }
    return $null
}

# A project entry: "name" (the copy linked globally) or "owner/repo:name" (that library's copy).
function Resolve-Entry {
    param([string]$Entry)
    if ($Entry -cmatch '^(.+):([a-z0-9]+(?:-[a-z0-9]+)*)$') {
        $id = $Matches[1]
        $name = $Matches[2]
        $library = $null
        foreach ($candidate in $script:catalog) {
            if ($candidate.repoId -and [string]::Equals($candidate.repoId, $id, [StringComparison]::OrdinalIgnoreCase)) { $library = $candidate; break }
        }
        return @{entry = $Entry; name = $name; library = $library; repoId = $id}
    }
    $library = $script:winners[$Entry]
    if (-not $library) {
        # A library that is only unavailable (a disconnected drive) has no winners, yet the
        # links recorded into it still say which copy the entry means.
        $state = Get-Variable -Name state -Scope Script -ValueOnly -ErrorAction SilentlyContinue
        foreach ($record in @(if ($state) { $state.links.Values })) {
            if ($record.skill -cne $Entry) { continue }
            $owner = Find-TargetLibrary $record.target
            if ($owner -and -not $owner.available) { $library = $owner; break }
        }
    }
    return @{entry = $Entry; name = $Entry; library = $library; repoId = $null}
}

function Read-Bindings {
    Initialize-Catalog
    # "library" names the first library for older engines; the state covers all of them.
    $primary = $repo
    if ($script:catalog.Count) { $primary = $script:catalog[0].path }
    if (-not (Test-Path -LiteralPath $bindingsFile)) {
        $script:state = @{version = 1; library = $primary; projects = @{}; global = @{}; links = @{}; excludes = @{}}
        $script:savedState = ''
        return
    }
    try { $loaded = ConvertTo-Plain ([IO.File]::ReadAllText($bindingsFile) | ConvertFrom-Json) } catch { $loaded = $null }
    if ($loaded -isnot [hashtable] -or $loaded.version -ne 1 -or $loaded.library -isnot [string] -or
        @('projects', 'global', 'links', 'excludes' | Where-Object { $loaded[$_] -isnot [hashtable] }).Count) {
        throw "Invalid local bindings state preserved: $bindingsFile"
    }
    $script:savedState = ConvertTo-Json -InputObject $loaded -Depth 10
    $loaded.library = $primary
    $script:state = $loaded
}

# Bindings span all libraries, so their changes wait for each other, not per library.
function Enter-BindingsLock {
    [IO.Directory]::CreateDirectory($cache) | Out-Null
    $path = Join-Path $cache 'bindings.lock'
    $timeout = 300
    if ($env:AGENT_SKILLS_LOCK_TIMEOUT) { $timeout = [int]$env:AGENT_SKILLS_LOCK_TIMEOUT }
    $deadline = [DateTime]::UtcNow.AddSeconds($timeout)
    while ($true) {
        try { return [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch [IO.IOException] {
            if ([DateTime]::UtcNow -gt $deadline) { throw 'Skill links are being updated by another run; nothing changed. Retry later.' }
            Start-Sleep -Milliseconds 200
        }
    }
}

function Save-Bindings {
    $json = ConvertTo-Json -InputObject $script:state -Depth 10
    if ($json -ceq $script:savedState) { return }
    [IO.Directory]::CreateDirectory((Split-Path -Parent $bindingsFile)) | Out-Null
    Write-TextFile -Path $bindingsFile -Text $json
    $script:savedState = $json
}

function Get-GlobalChoice {
    param([string]$Name)
    if ($script:state.global.ContainsKey($Name)) { return $script:state.global[$Name] }
    return @{enabled = $true; manual = $false; pending = $null}
}

function Test-LibrarySkill {
    param([string]$Name, $Library, [switch]$Quiet)
    if (-not $Library) { $Library = $script:winners[$Name] }
    if (-not $Library -or -not $Library.available) {
        if (-not $Quiet) { Write-Warning "Skill is not in a connected library: $Name" }
        return $false
    }
    try {
        Assert-CatalogSkill -Name $Name -Root $Library.path
        return $true
    } catch {
        if (-not $Quiet) { Write-Warning $_.Exception.Message }
        return $false
    }
}

function Get-ProjectKey {
    param([string]$Path)
    $full = [IO.Path]::GetFullPath([IO.Path]::Combine((Get-Location).ProviderPath, $Path))
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) { $full = $full.TrimEnd('\', '/') }
    return $full
}

function Read-ProjectList {
    param([string]$Project)
    $file = Join-Path $Project $projectList
    if (-not (Test-Path -LiteralPath $file)) { return }
    try { $data = [IO.File]::ReadAllText($file) | ConvertFrom-Json } catch { $data = $null }
    $valid = $data -is [Management.Automation.PSCustomObject] -and $data.version -is [int] -and $data.version -eq 1 -and
        $data.skills -is [Array] -and -not @($data.PSObject.Properties.Name | Where-Object { $_ -cnotin @('version', 'skills') }).Count
    if ($valid) {
        foreach ($name in $data.skills) {
            # "name", or "owner/repo:name" for the copy of one library.
            if ($name -isnot [string] -or $name -cnotmatch '^(?:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+:)?([a-z0-9]+(-[a-z0-9]+)*)$' -or
                $name.Length - $name.LastIndexOf(':') - 1 -gt 64) { $valid = $false }
        }
    }
    if (-not $valid) { throw "Invalid project skill list preserved: $file" }
    [string[]]$names = @($data.skills | Select-Object -Unique)
    [Array]::Sort($names, [StringComparer]::Ordinal)
    return $names
}

# Current list of an available project, or the last one seen while it was available.
function Get-ProjectSkills {
    param([string]$Project)
    if (Test-Path -LiteralPath $Project -PathType Container) {
        try { return @(Read-ProjectList $Project) } catch { }
    }
    if ($script:state.projects.ContainsKey($Project)) { return @($script:state.projects[$Project].skills) }
    return @()
}

function Write-ProjectList {
    param([string]$Project, [string[]]$Names)
    [string[]]$sorted = @($Names | Where-Object { $_ } | Select-Object -Unique)
    [Array]::Sort($sorted, [StringComparer]::Ordinal)
    $text = "{`n  `"version`": 1,`n  `"skills`": []`n}`n"
    if ($sorted.Count) {
        $items = ($sorted | ForEach-Object { "    `"$_`"" }) -join ",`n"
        $text = "{`n  `"version`": 1,`n  `"skills`": [`n$items`n  ]`n}`n"
    }
    $file = Join-Path $Project $projectList
    if ([IO.File]::Exists($file) -and [IO.File]::ReadAllText($file) -ceq $text) { return }
    # Only the working tree changes: staging and commits stay with the project workflow.
    Write-TextFile -Path $file -Text $text
}

function Invoke-QuietGit {
    param([string]$Directory, [string[]]$GitArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & git -C $Directory @GitArgs 2>$null } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { return $null }
    return $output
}

# Exclude file and path prefix of a Git work tree; empty outside Git.
function Get-ExcludeLocation {
    param([string]$Project)
    if ((Invoke-QuietGit -Directory $Project -GitArgs @('rev-parse', '--is-inside-work-tree')) -ne 'true') {
        return @{file = ''; prefix = ''}
    }
    $file = Invoke-Git -Directory $Project -GitArgs @('rev-parse', '--path-format=absolute', '--git-path', 'info/exclude')
    $prefix = [string](Invoke-Git -Directory $Project -GitArgs @('rev-parse', '--show-prefix'))
    return @{file = [IO.Path]::GetFullPath($file); prefix = [regex]::Replace($prefix, '[\[\]*?\\]', '\$0')}
}

function Add-Exclude {
    param([string]$File, [string]$Line)
    if (-not $File) { return }
    $text = ''
    if ([IO.File]::Exists($File)) {
        $text = [IO.File]::ReadAllText($File)
        if (@($text -split "`n" | ForEach-Object { $_.TrimEnd("`r") }) -ccontains $Line) { return }
    }
    $prefix = ''
    if ($text -and -not $text.EndsWith("`n")) { $prefix = "`n" }
    [IO.Directory]::CreateDirectory((Split-Path -Parent $File)) | Out-Null
    [IO.File]::AppendAllText($File, "$prefix$Line`n", $utf8)
    $owned = @()
    if ($script:state.excludes.ContainsKey($File)) { $owned = @($script:state.excludes[$File]) }
    if ($owned -cnotcontains $Line) { $script:state.excludes[$File] = @($owned + $Line) }
}

# Remove only exclude lines this client added and no recorded link still needs.
function Remove-UnusedExcludes {
    $used = New-Object 'Collections.Generic.HashSet[string]'
    foreach ($record in $script:state.links.Values) {
        if ($record.exclude) { [void]$used.Add($record.exclude.file.ToLowerInvariant() + "`t" + $record.exclude.line) }
    }
    $failed = $false
    foreach ($file in @($script:state.excludes.Keys)) {
        $kept = @()
        foreach ($line in @($script:state.excludes[$file])) {
            if ($used.Contains($file.ToLowerInvariant() + "`t" + $line)) { $kept += $line; continue }
            try {
                if ([IO.File]::Exists($file)) {
                    $lines = [Collections.Generic.List[string]]([IO.File]::ReadAllText($file) -split "`n")
                    for ($index = 0; $index -lt $lines.Count; $index++) {
                        if ($lines[$index].TrimEnd("`r") -ceq $line) { $lines.RemoveAt($index); break }
                    }
                    Write-TextFile -Path $file -Text ($lines -join "`n")
                }
            } catch { Write-Warning $_.Exception.Message; $failed = $true; $kept += $line }
        }
        if ($kept.Count) { $script:state.excludes[$file] = $kept } else { $script:state.excludes.Remove($file) }
    }
    return -not $failed
}

function Get-LinkInfo {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return $null }
    $target = $null
    if ($item.LinkType -in @('Junction', 'SymbolicLink')) {
        $target = [string]@($item.Target)[0]
        if (-not [IO.Path]::IsPathRooted($target)) { $target = Join-Path (Split-Path -Parent $Path) $target }
        $target = [IO.Path]::GetFullPath($target).TrimEnd('\', '/')
    }
    return @{target = $target}
}

function Get-LinkState {
    param([string]$Path, [string]$Target)
    $info = Get-LinkInfo $Path
    if (-not $info) { return 'missing' }
    if ($info.target -and (Test-SamePath $info.target $Target)) { return 'linked' }
    return 'conflict'
}

function Get-SkillTarget {
    param([string]$Name, $Library)
    if (-not $Library) { $Library = $script:winners[$Name] }
    $root = $repo
    if ($Library) { $root = $Library.path }
    return [IO.Path]::GetFullPath([IO.Path]::Combine($root, 'skills', $Name))
}

function Get-AgentLink {
    param([string]$Root, [string]$Agent, [string]$Name)
    return Join-Path (Join-Path (Join-Path $Root $Agent) 'skills') $Name
}

function Set-LinkRecord {
    param([string]$Link, [string]$Target, [string]$Name, [string]$Project, [string]$File, [string]$Line)
    $exclude = $null
    if ($File) { $exclude = @{file = $File; line = $Line} }
    $projectValue = $null
    if ($Project) { $projectValue = $Project }
    $script:state.links[$Link] = @{target = $Target; skill = $Name; project = $projectValue; exclude = $exclude}
}

function Remove-LinkItem {
    param([string]$Path)
    [IO.Directory]::Delete($Path)
}

# Removes a link only while it still points where this client created it.
function Remove-OwnedLink {
    param([string]$Link)
    if (-not $script:state.links.ContainsKey($Link)) { return $true }
    $recorded = $script:state.links[$Link].target
    $info = Get-LinkInfo $Link
    if ($info -and $info.target -and (Test-SamePath $info.target $recorded)) {
        try { Remove-LinkItem $Link } catch { Write-Warning $_.Exception.Message; return $false }
        Write-Host "UNLINKED: $Link"
    } elseif ($info) {
        Write-Warning "Changed link preserved: $Link"
    }
    $script:state.links.Remove($Link)
    return $true
}

function Set-ProjectSkillLinks {
    param([string]$Project, [hashtable]$Item, [hashtable]$Exclude)
    $Name = $Item.name
    $target = Get-SkillTarget $Name $Item.library
    $applied = $true
    foreach ($agent in $agentFolders) {
        $link = Get-AgentLink $Project $agent $Name
        $line = "/$($Exclude.prefix)$agent/skills/$Name"
        try {
            $linkState = Get-LinkState $link $target
            if ($linkState -eq 'conflict') { Write-Warning "CONFLICT (kept): $link"; $applied = $false; continue }
            # Exclude first so Git never reports the machine-specific link.
            Add-Exclude -File $Exclude.file -Line $line
            if ($linkState -eq 'missing') {
                [IO.Directory]::CreateDirectory((Split-Path -Parent $link)) | Out-Null
                New-SkillLink -Path $link -Target $target
                Write-Host "LINKED: $link"
            }
            Set-LinkRecord -Link $link -Target (Get-LinkInfo $link).target -Name $Item.entry -Project $Project -File $Exclude.file -Line $line
        } catch { Write-Warning "Link failed: $link ($($_.Exception.Message))"; $applied = $false }
    }
    return $applied
}

function Update-Project {
    param([string]$Project)
    if (-not (Test-Path -LiteralPath $Project -PathType Container)) {
        Write-Warning "UNAVAILABLE (kept): $Project"
        $script:state.projects[$Project].available = $false
        return $true
    }
    try { $names = @(Read-ProjectList $Project) }
    catch { Write-Warning $_.Exception.Message; $script:state.projects[$Project].available = $true; return $false }
    $exclude = Get-ExcludeLocation $Project
    $script:state.projects[$Project] = @{available = $true; skills = $names}
    $ok = $true
    $items = @()
    $taken = @{}
    foreach ($entry in $names) {
        $item = Resolve-Entry $entry
        if ($taken.ContainsKey($item.name)) {
            Write-Warning "DUPLICATE (not applied): $entry in $Project; $($taken[$item.name]) is bound already"; $ok = $false; continue
        }
        $taken[$item.name] = $entry
        $items += $item
    }
    # A link stays while its entry still resolves to the same copy; an offline library keeps it.
    foreach ($link in @($script:state.links.Keys)) {
        $record = $script:state.links[$link]
        if (-not $record.project -or -not (Test-SamePath $record.project $Project)) { continue }
        $keep = $false
        foreach ($item in $items) {
            if ($record.skill -cne $item.entry -or -not $item.library) { continue }
            if (-not $item.library.available) { $keep = $true; continue }
            if (@($item.library.skills) -ccontains $item.name -and (Test-SamePath $record.target (Get-SkillTarget $item.name $item.library))) { $keep = $true }
        }
        if (-not $keep) { if (-not (Remove-OwnedLink $link)) { $ok = $false } }
    }
    foreach ($item in $items) {
        if (-not $item.library) {
            Write-Warning "LIBRARY NOT CONNECTED (not applied): $($item.entry) in $Project"; $ok = $false; continue
        }
        if (-not $item.library.available) { Write-Warning "LIBRARY UNAVAILABLE (kept): $($item.entry) in $Project"; continue }
        if (-not (Test-LibrarySkill -Name $item.name -Library $item.library -Quiet)) {
            Write-Warning "NOT IN LIBRARY (not applied): $($item.entry) in $Project"; $ok = $false; continue
        }
        if (Set-ProjectSkillLinks -Project $Project -Item $item -Exclude $exclude) {
            # The automatic global choice of a UI connection waits for its success.
            $choice = Get-GlobalChoice $item.name
            if ($choice.pending -and (Test-SamePath $choice.pending $Project)) {
                $script:state.global[$item.name] = @{enabled = $false; manual = $false; pending = $null}
            }
        } else { $ok = $false }
    }
    return $ok
}

function Set-GlobalLinks {
    param([string]$Name)
    $winner = $script:winners[$Name]
    $target = Get-SkillTarget $Name $winner
    $ok = $true
    if ((Get-GlobalChoice $Name).enabled) {
        # A link someone else made to the same folder stays theirs: only links this run
        # created, or ones recorded before, become the client's own.
        $foreign = @{}
        foreach ($agent in $agentFolders) {
            $link = Get-AgentLink $HOME $agent $Name
            if ((Get-LinkInfo $link) -and -not $script:state.links.ContainsKey($link)) { $foreign[$link] = $true }
        }
        try { Set-WinnerLinks -Name $Name -Library $winner } catch { Write-Warning $_.Exception.Message; $ok = $false }
        foreach ($agent in $agentFolders) {
            $link = Get-AgentLink $HOME $agent $Name
            if (-not $foreign.ContainsKey($link) -and (Get-LinkState $link $target) -eq 'linked') {
                Set-LinkRecord -Link $link -Target (Get-LinkInfo $link).target -Name $Name
            }
        }
        return $ok
    }
    foreach ($agent in $agentFolders) {
        $link = Get-AgentLink $HOME $agent $Name
        $linkState = Get-LinkState $link $target
        $record = $script:state.links[$link]
        if ($linkState -eq 'linked' -and $record -and (Test-SamePath $record.target (Get-LinkInfo $link).target)) {
            try { Remove-LinkItem $link; Write-Host "UNLINKED: $link" } catch { Write-Warning $_.Exception.Message; $ok = $false; continue }
        } elseif ($linkState -ne 'missing') {
            Write-Warning "Foreign global install kept: $link"
        }
        $script:state.links.Remove($link)
    }
    return $ok
}

function Set-WinnerLinks {
    param([string]$Name, $Library)
    $repo = $Library.path
    Set-SkillLinks -Name $Name
}

# Global links whose copy no longer wins its name, of a skill the library dropped, or into
# a vanished temporary library. Links of an offline or foreign library are kept.
function Remove-StaleLinks {
    $ok = $true
    foreach ($link in @($script:state.links.Keys)) {
        $record = $script:state.links[$link]
        if ($record.project) { continue }
        $owner = Find-TargetLibrary $record.target
        if ($owner) {
            if (-not $owner.available) { continue }
            $winner = $script:winners[$record.skill]
            if ($winner -and (Test-SamePath $winner.path $owner.path)) { continue }
        } elseif (-not (Test-DisposableLibrary $record.target)) { continue }
        if (-not (Remove-OwnedLink $link)) { $ok = $false }
    }
    return $ok
}

# Brings links of every registered project and global folder to the saved choices.
function Sync-Bindings {
    $ok = $true
    foreach ($project in @($script:state.projects.Keys)) {
        if (-not (Update-Project $project)) { $ok = $false }
    }
    # A pending automatic choice is dropped together with the binding that requested it.
    foreach ($name in @($script:state.global.Keys)) {
        $pending = $script:state.global[$name].pending
        if ($pending -and -not ($script:state.projects.ContainsKey($pending) -and
            @($script:state.projects[$pending].skills | ForEach-Object { (Resolve-Entry $_).name }) -ccontains $name)) {
            $script:state.global[$name].pending = $null
        }
    }
    if (-not (Remove-StaleLinks)) { $ok = $false }
    foreach ($name in @($script:winners.Keys | Sort-Object)) {
        if (-not (Set-GlobalLinks $name)) { $ok = $false }
    }
    if (-not (Remove-UnusedExcludes)) { $ok = $false }
    Save-Bindings
    return $ok
}

# A UI connection hides a default global skill once the connection is applied.
function Request-AutoOff {
    param([string]$Name, [string]$Project)
    $choice = Get-GlobalChoice $Name
    if ($choice.manual -or -not $choice.enabled -or $choice.pending) { return }
    $script:state.global[$Name] = @{enabled = $true; manual = $false; pending = $Project}
}

function Set-GlobalChoice {
    param([string]$Name, [bool]$Enabled)
    $script:state.global[$Name] = @{enabled = $Enabled; manual = $true; pending = $null}
}

function Test-BindingApplied {
    param([string]$Project, [string]$Name)
    $item = Resolve-Entry $Name
    if (-not $item.library) { return $false }
    foreach ($agent in $agentFolders) {
        if ((Get-LinkState (Get-AgentLink $Project $agent $item.name) (Get-SkillTarget $item.name $item.library)) -ne 'linked') { return $false }
    }
    return $true
}

function Resolve-RegisteredProject {
    param([string]$Path)
    $key = Get-ProjectKey $Path
    foreach ($project in $script:state.projects.Keys) { if (Test-SamePath $project $key) { return $project } }
    throw "Project is not registered: $Path"
}

function Assert-ProjectAvailable {
    param([string]$Project)
    if (-not (Test-Path -LiteralPath $Project -PathType Container)) { throw "Project folder unavailable; registration kept: $Project" }
}

# Each command returns the process exit code; see projects.ps1.
function Invoke-Register {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "Project folder not found: $Path" }
    $project = Get-ProjectKey $Path
    foreach ($library in $script:catalog) {
        $root = $library.path.TrimEnd('\', '/')
        if ((Test-SamePath $project $root) -or $project.StartsWith($root + [IO.Path]::DirectorySeparatorChar, $pathComparison)) {
            throw 'The library itself cannot be a project.'
        }
    }
    foreach ($known in $script:state.projects.Keys) { if (Test-SamePath $known $project) { $project = $known } }
    $names = @(Read-ProjectList $project)
    if (-not $script:state.projects.ContainsKey($project)) { $script:state.projects[$project] = @{available = $true; skills = @()} }
    foreach ($name in $names) { Request-AutoOff -Name (Resolve-Entry $name).name -Project $project }
    Save-Bindings
    if (-not (Sync-Bindings)) { Write-Warning 'Some bindings need attention; see status.' }
    foreach ($name in $names) {
        if (-not (Test-BindingApplied $project $name)) { Write-Warning "Binding not applied: $name in $project"; return 4 }
    }
    return 0
}

function Invoke-Unregister {
    param([string]$Path)
    $project = Resolve-RegisteredProject $Path
    $available = Test-Path -LiteralPath $project -PathType Container
    foreach ($link in @($script:state.links.Keys)) {
        $record = $script:state.links[$link]
        if (-not $record.project -or -not (Test-SamePath $record.project $project)) { continue }
        if ($available) { if (-not (Remove-OwnedLink $link)) { return 1 } }
        else { $script:state.links.Remove($link) }
    }
    $script:state.projects.Remove($project)
    foreach ($choice in $script:state.global.Values) {
        if ($choice.pending -and (Test-SamePath $choice.pending $project)) { $choice.pending = $null }
    }
    Save-Bindings
    [void](Sync-Bindings)
    return 0
}

function Invoke-Bind {
    param([string]$Path, [string]$Name, [string]$Library)
    $project = Resolve-RegisteredProject $Path
    Assert-ProjectAvailable $project
    $entry = $Name
    if ($Library) {
        # A named library writes "owner/repo:name", the same for every colleague.
        $chosen = Find-CatalogLibrary $Library
        if (-not $chosen) { throw "Library is not connected: $Library" }
        if (-not $chosen.repoId) { throw "Library $Library has no origin; a project cannot name it." }
        if (-not (Test-LibrarySkill -Name $Name -Library $chosen)) { throw "Skill is not in library ${Library}: $Name" }
        $entry = "$($chosen.repoId):$Name"
    } elseif (-not (Test-LibrarySkill -Name $Name)) { throw "Skill is not in a connected library: $Name" }
    # Another copy of the same skill in this project is replaced.
    $names = @(Read-ProjectList $project | Where-Object { (Resolve-Entry $_).name -cne $Name })
    Request-AutoOff -Name $Name -Project $project
    Save-Bindings
    Write-ProjectList -Project $project -Names (@($names) + $entry)
    if (-not (Sync-Bindings)) { Write-Warning 'Some other bindings need attention; see status.' }
    if (-not (Test-BindingApplied $project $entry)) { Write-Warning "Binding saved but not applied: $entry in $project"; return 4 }
    return 0
}

function Invoke-Unbind {
    param([string]$Path, [string]$Name, [string]$Choice)
    if ($Choice -and $Choice -cnotin @('global', 'library')) { throw "Unknown choice: $Choice" }
    $project = Resolve-RegisteredProject $Path
    Assert-ProjectAvailable $project
    $names = @(Read-ProjectList $project)
    # The plain skill name also removes its library-qualified entry.
    if ($names -cnotcontains $Name) {
        $match = @($names | Where-Object { (Resolve-Entry $_).name -ceq $Name })
        if ($match.Count -eq 1) { $Name = $match[0] }
    }
    $skill = (Resolve-Entry $Name).name
    if ($names -ccontains $Name -and -not $Choice) {
        $others = @($script:state.projects.Keys | Where-Object {
            -not (Test-SamePath $_ $project) -and @(Get-ProjectSkills $_ | ForEach-Object { (Resolve-Entry $_).name }) -ccontains $skill })
        if (-not $others.Count) {
            # The UI asks; cancelling keeps the binding because nothing changed yet.
            $decision = [ordered]@{decision = 'last-project'; skill = $skill; project = $project
                global = (Get-GlobalChoice $skill); choices = @('global', 'library')}
            Write-Json $decision
            return 3
        }
    }
    if ($Choice) {
        Set-GlobalChoice -Name $skill -Enabled ($Choice -ceq 'global')
        Save-Bindings
    }
    Write-ProjectList -Project $project -Names @($names | Where-Object { $_ -cne $Name })
    if (-not (Sync-Bindings)) { Write-Warning 'Some other bindings need attention; see status.' }
    foreach ($agent in $agentFolders) {
        if ($script:state.links.ContainsKey((Get-AgentLink $project $agent $skill))) { return 1 }
    }
    return 0
}

function Invoke-Global {
    param([string]$Name, [string]$Value)
    if ($Value -cnotin @('on', 'off')) { Write-Warning 'Expected on or off.'; return 2 }
    if (-not (Test-LibrarySkill -Name $Name)) { throw "Skill is not in the library: $Name" }
    Set-GlobalChoice -Name $Name -Enabled ($Value -ceq 'on')
    Save-Bindings
    if (-not (Sync-Bindings)) { Write-Warning 'Some other bindings need attention; see status.' }
    foreach ($agent in $agentFolders) {
        $linkState = Get-LinkState (Get-AgentLink $HOME $agent $Name) (Get-SkillTarget $Name)
        if (($Value -ceq 'on') -ne ($linkState -eq 'linked')) { return 1 }
    }
    return 0
}

function Invoke-Status {
    $primary = $repo
    $projects = @()
    foreach ($project in @($script:state.projects.Keys | Sort-Object)) {
        $available = Test-Path -LiteralPath $project -PathType Container
        $errorValue = $null
        if (-not $available) { $list = @(Get-ProjectSkills $project) }
        else {
            try { $list = @(Read-ProjectList $project) }
            catch { $errorValue = 'invalid-list'; $list = @(Get-ProjectSkills $project) }
        }
        $skills = @()
        foreach ($entry in $list) {
            $item = Resolve-Entry $entry
            $libraryName = $null
            if ($item.library) { $libraryName = $item.library.name }
            $links = [ordered]@{}
            $stateName = 'applied'
            if (-not $available) { $stateName = 'unavailable' }
            elseif (-not $item.library) { $stateName = 'library-missing' }
            elseif (-not $item.library.available) { $stateName = 'library-unavailable' }
            elseif (-not (Test-LibrarySkill -Name $item.name -Library $item.library -Quiet)) { $stateName = 'missing-skill' }
            else {
                foreach ($agent in $agentFolders) {
                    $link = Get-AgentLink $project $agent $item.name
                    $links[$link] = Get-LinkState $link (Get-SkillTarget $item.name $item.library)
                    if ($links[$link] -eq 'conflict') { $stateName = 'conflict' }
                    elseif ($links[$link] -eq 'missing' -and $stateName -eq 'applied') { $stateName = 'pending' }
                }
            }
            $skills += [ordered]@{name = $entry; skill = $item.name; library = $libraryName; state = $stateName; links = $links}
        }
        $projects += [ordered]@{path = $project; available = $available; error = $errorValue; skills = $skills}
    }
    $rows = @()
    $libraries = @()
    foreach ($library in $script:catalog) {
        $libraries += Get-LibraryStatus $library
        if (-not $library.available) { continue }
        $repo = $library.path
        # External source and update policy; pending: a local switch not yet merged into main.
        $localSkills = $null
        $remoteSkills = $null
        try { $localSkills = (Get-Content -LiteralPath (Join-Path $repo 'sources.json') -Raw -Encoding UTF8 | ConvertFrom-Json).skills } catch { }
        try { $remoteSkills = ((Invoke-Git -GitArgs @('show', "refs/remotes/origin/${mainBranch}:sources.json") 2>$null) -join "`n" | ConvertFrom-Json).skills } catch { }
        foreach ($name in $library.skills) {
            $winner = $script:winners[$name]
            $active = [bool]($winner -and (Test-SamePath $winner.path $library.path))
            $update = $null
            $entry = $null
            if ($localSkills) { $entry = $localSkills.$name }
            if ($entry -and $entry.source.kind -ne 'local') {
                $where = [string]$entry.source.url
                if ($entry.source.path -and $entry.source.path -ne '.') { $where += ' ' + $entry.source.path }
                $merged = if ($remoteSkills -and $remoteSkills.$name) { [bool]$remoteSkills.$name.auto_update } else { [bool]$entry.auto_update }
                $update = [ordered]@{source = $where; policy = $(if ($entry.auto_update) { 'auto' } else { 'confirm' })
                    pending = ($merged -ne [bool]$entry.auto_update)}
            }
            $choice = Get-GlobalChoice $name
            $links = [ordered]@{}
            foreach ($agent in $agentFolders) {
                $link = Get-AgentLink $HOME $agent $name
                $links[$link] = Get-LinkState $link (Get-SkillTarget $name $library)
            }
            $bound = @($projects | Where-Object { @($_.skills | Where-Object { $_.skill -ceq $name -and $_.library -ceq $library.name }).Count } |
                ForEach-Object { $_.path })
            $key = $name
            if (-not $active -and $library.repoId) { $key = "$($library.repoId):$name" }
            $shadowedBy = $null
            if (-not $active -and $winner) { $shadowedBy = $winner.name }
            $copies = @($script:copies[$name] | ForEach-Object { $_.name })
            $rows += [ordered]@{name = $name; library = $library.name; entry = $key; active = $active; shadowedBy = $shadowedBy
                copies = $copies; preferred = [bool]$script:preferred.ContainsKey($name); projects = $bound; update = $update
                global = [ordered]@{enabled = $choice.enabled; manual = $choice.manual; pending = $choice.pending; links = $links}}
        }
    }
    $repo = $primary
    # Nothing is connected once the last library was disconnected.
    $format = $null
    if ($repo) {
        $value = [Math]::Max((Get-LibraryFormat), (Get-LibraryFormat "refs/remotes/origin/$mainBranch"))
        $format = [ordered]@{library = $value; supported = $supportedFormat; readOnly = ($value -gt $supportedFormat)}
    }
    Write-Json ([ordered]@{library = $repo; state = $bindingsFile; format = $format
        libraries = $libraries; skills = $rows; projects = $projects})
    return 0
}

# One library for status: order, origin, format and the result of its last synchronization.
function Get-LibraryStatus {
    param($Library)
    $format = $null
    $sync = $null
    # A library whose registry cannot be read is unavailable for linking, yet its folder is there.
    if (Test-Path -LiteralPath $Library.path -PathType Container) {
        $repo = $Library.path
        try {
            $value = [Math]::Max((Get-LibraryFormat), (Get-LibraryFormat "refs/remotes/origin/$mainBranch"))
            $format = [ordered]@{library = $value; supported = $supportedFormat; readOnly = ($value -gt $supportedFormat)}
        } catch { }
        $sync = Read-SyncStatus $Library.path
    }
    return [ordered]@{name = $Library.name; path = $Library.path; url = $Library.url; repo = $Library.repoId
        available = $Library.available; configured = $Library.configured; skills = @($Library.skills).Count; format = $format; sync = $sync}
}

# --- Connected libraries: order, preferred copies, disconnection -------------------------

function Get-SyncStatusPath {
    param([string]$Path)
    $directory = Invoke-QuietGit -Directory $Path -GitArgs @('rev-parse', '--absolute-git-dir')
    if (-not $directory) { return $null }
    return (Join-Path ([string]$directory) 'agent-skills-sync-status.json')
}

function Write-SyncStatus {
    param([string]$Path, [bool]$Ok, [string]$Message)
    $file = Get-SyncStatusPath $Path
    if (-not $file) { return }
    $value = [ordered]@{time = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); ok = $Ok; error = $null}
    if (-not $Ok) { $value.error = $Message }
    try { Write-TextFile -Path $file -Text (ConvertTo-Json -InputObject $value -Compress) } catch { Write-Warning $_.Exception.Message }
}

function Read-SyncStatus {
    param([string]$Path)
    $file = Get-SyncStatusPath $Path
    if (-not $file -or -not [IO.File]::Exists($file)) { return $null }
    try { return ([IO.File]::ReadAllText($file) | ConvertFrom-Json) } catch { return $null }
}

function Assert-ConfiguredLibrary {
    param([string]$Name)
    $found = Find-ConfiguredLibrary $Name
    if (-not $found) { throw "Library is not connected: $Name (connected: $((@(Get-ConfiguredLibraries) | ForEach-Object { $_.name }) -join ', '))" }
    return $found
}

function Invoke-Libraries {
    if (-not $script:catalog.Count) { Write-Host 'No library is connected.'; return 0 }
    $index = 0
    foreach ($library in $script:catalog) {
        $index++
        $line = "$index. $($library.name)  $($library.path)"
        if ($library.repoId) { $line += "  ($($library.repoId))" }
        if (-not $library.configured) { $line += '  [this command only]' }
        if (-not $library.available) { $line += '  UNAVAILABLE' }
        else {
            $sync = Read-SyncStatus $library.path
            if ($sync -and $sync.ok) { $line += "  synced $($sync.time)" }
            elseif ($sync) { $line += "  SYNC FAILED $($sync.time): $($sync.error)" }
        }
        Write-Host $line
    }
    $shadowed = @($script:copies.Keys | Where-Object { $script:copies[$_].Count -gt 1 } | Sort-Object)
    foreach ($name in $shadowed) {
        $mark = ''
        if ($script:preferred.ContainsKey($name)) { $mark = ' (preferred)' }
        Write-Host "CONFLICT ${name}: linked from $($script:winners[$name].name)$mark; also in $((@($script:copies[$name] | Where-Object { $_ -ne $script:winners[$name] } | ForEach-Object { $_.name })) -join ', ')"
    }
    return 0
}

function Invoke-Order {
    param([string[]]$Names)
    $configured = @(Get-ConfiguredLibraries)
    foreach ($name in $Names) { [void](Assert-ConfiguredLibrary $name) }
    if (@($Names | Select-Object -Unique).Count -ne $Names.Count) { throw 'Each library is named once.' }
    $ordered = @($Names | ForEach-Object { $name = $_; $configured | Where-Object { $_.name -ceq $name } })
    $ordered += @($configured | Where-Object { $Names -cnotcontains $_.name })
    Write-SkillariumConfig (Read-SkillariumConfig) $ordered (Get-PreferredLibraries)
    Initialize-Catalog
    $ok = Sync-Bindings
    [void](Invoke-Libraries)
    if (-not $ok) { Write-Warning 'Some links need attention; see status.'; return 4 }
    return 0
}

function Invoke-Prefer {
    param([string]$Name, [string]$Library)
    $prefer = Get-PreferredLibraries
    if ($Library -ceq 'auto') { $prefer.Remove($Name) }
    else {
        [void](Assert-ConfiguredLibrary $Library)
        if (-not ($script:copies.ContainsKey($Name) -and @($script:copies[$Name] | Where-Object { $_.name -ceq $Library }).Count)) {
            throw "Skill $Name is not in library $Library."
        }
        $prefer[$Name] = $Library
    }
    Write-SkillariumConfig (Read-SkillariumConfig) @(Get-ConfiguredLibraries) $prefer
    Initialize-Catalog
    $ok = Sync-Bindings
    if ($script:winners[$Name]) { Write-Host "$Name is linked from $($script:winners[$Name].name)" }
    if (-not $ok) { Write-Warning 'Some links need attention; see status.'; return 4 }
    return 0
}

# Removes the library's links and its configuration entry; the clone stays on disk.
function Invoke-Disconnect {
    param([string]$Name, [string]$Flag)
    if ($Flag -and $Flag -cnotin @('--force', '-Force')) { throw "Unknown option: $Flag" }
    $configured = @(Get-ConfiguredLibraries)
    $target = Assert-ConfiguredLibrary $Name
    $library = Find-CatalogLibrary $Name
    if (-not $Flag -and (Test-Path -LiteralPath $library.path -PathType Container)) {
        $changes = @(Invoke-QuietGit -Directory $library.path -GitArgs @('status', '--porcelain', '--', 'skills', 'sources.json'))
        $ahead = @(Invoke-QuietGit -Directory $library.path -GitArgs @('rev-list', '@{upstream}..HEAD'))
        if (@($changes | Where-Object { $_ }).Count -or @($ahead | Where-Object { $_ }).Count) {
            throw "Library $Name has unpublished changes; let them publish (sync) first, or pass --force. The clone stays in $($library.path) either way."
        }
    }
    $ok = $true
    $prefix = [IO.Path]::GetFullPath([IO.Path]::Combine($library.path, 'skills')).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    foreach ($link in @($script:state.links.Keys)) {
        $recorded = [string]$script:state.links[$link].target
        if ($recorded.StartsWith($prefix, $pathComparison)) { if (-not (Remove-OwnedLink $link)) { $ok = $false } }
    }
    # Links this client did not record are not its own: they stay, with a note.
    foreach ($skill in @($library.skills)) {
        foreach ($agent in $agentFolders) {
            $link = Get-AgentLink $HOME $agent $skill
            $info = Get-LinkInfo $link
            if ($info -and $info.target -and $info.target.StartsWith($prefix, $pathComparison)) {
                Write-Warning "Unrecorded link into the disconnected library kept: $link"
            }
        }
    }
    if (-not $ok) { Save-Bindings; throw "Some links of $Name could not be removed; the library stays connected." }
    $prefer = Get-PreferredLibraries
    foreach ($skill in @($prefer.Keys)) { if ($prefer[$skill] -ceq $Name) { $prefer.Remove($skill) } }
    Write-SkillariumConfig (Read-SkillariumConfig) @($configured | Where-Object { $_.name -cne $Name }) $prefer
    if ($commandLibrary -and (Test-SamePath $commandLibrary $library.path)) { $script:commandLibrary = '' }
    Initialize-Catalog
    if ($script:catalog.Count) { $script:state.library = $script:catalog[0].path }
    if (-not (Sync-Bindings)) { Write-Warning 'Some links need attention; see status.' }
    Write-Host "DISCONNECTED: $Name; the clone stays in $($library.path)"
    return 0
}
