# Local skill availability: global links and project bindings of this library.
# Project lists live in each project's Git; registered paths, global choices
# and link ownership stay in local state that is never published.
$bindingsFile = Join-Path $cache 'bindings.json'
$projectList = '.agent-skills.json'
$agentFolders = @('.agents', '.claude')
$utf8 = New-Object Text.UTF8Encoding($false)
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

function Read-Bindings {
    if (-not (Test-Path -LiteralPath $bindingsFile)) {
        $script:state = @{version = 1; library = $repo; projects = @{}; global = @{}; links = @{}; excludes = @{}}
        $script:savedState = ''
        return
    }
    try { $loaded = ConvertTo-Plain ([IO.File]::ReadAllText($bindingsFile) | ConvertFrom-Json) } catch { $loaded = $null }
    if ($loaded -isnot [hashtable] -or $loaded.version -ne 1 -or $loaded.library -isnot [string] -or
        @('projects', 'global', 'links', 'excludes' | Where-Object { $loaded[$_] -isnot [hashtable] }).Count) {
        throw "Invalid local bindings state preserved: $bindingsFile"
    }
    if (-not (Test-SamePath $loaded.library $repo)) { throw "Local bindings belong to another library: $($loaded.library)" }
    $script:state = $loaded
    $script:savedState = ConvertTo-Json -InputObject $loaded -Depth 10
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
    param([string]$Name, [switch]$Quiet)
    try {
        Assert-CatalogSkill -Name $Name
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
            if ($name -isnot [string] -or $name.Length -gt 64 -or $name -cnotmatch '^[a-z0-9]+(-[a-z0-9]+)*$') { $valid = $false }
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
    param([string]$Name)
    return [IO.Path]::GetFullPath((Join-Path (Join-Path $repo 'skills') $Name))
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
    param([string]$Project, [string]$Name, [hashtable]$Exclude)
    $target = Get-SkillTarget $Name
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
            Set-LinkRecord -Link $link -Target (Get-LinkInfo $link).target -Name $Name -Project $Project -File $Exclude.file -Line $line
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
    foreach ($link in @($script:state.links.Keys)) {
        $record = $script:state.links[$link]
        if ($record.project -and (Test-SamePath $record.project $Project) -and $names -cnotcontains $record.skill) {
            if (-not (Remove-OwnedLink $link)) { $ok = $false }
        }
    }
    foreach ($name in $names) {
        if (-not (Test-LibrarySkill -Name $name -Quiet)) {
            Write-Warning "NOT IN LIBRARY (not applied): $name in $Project"; $ok = $false; continue
        }
        if (Set-ProjectSkillLinks -Project $Project -Name $name -Exclude $exclude) {
            # The automatic global choice of a UI connection waits for its success.
            $choice = Get-GlobalChoice $name
            if ($choice.pending -and (Test-SamePath $choice.pending $Project)) {
                $script:state.global[$name] = @{enabled = $false; manual = $false; pending = $null}
            }
        } else { $ok = $false }
    }
    return $ok
}

function Set-GlobalLinks {
    param([string]$Name)
    $target = Get-SkillTarget $Name
    $ok = $true
    if ((Get-GlobalChoice $Name).enabled) {
        try { Set-SkillLinks -Name $Name } catch { Write-Warning $_.Exception.Message; $ok = $false }
        foreach ($agent in $agentFolders) {
            $link = Get-AgentLink $HOME $agent $Name
            if ((Get-LinkState $link $target) -eq 'linked') { Set-LinkRecord -Link $link -Target (Get-LinkInfo $link).target -Name $Name }
        }
        return $ok
    }
    foreach ($agent in $agentFolders) {
        $link = Get-AgentLink $HOME $agent $Name
        $linkState = Get-LinkState $link $target
        if ($linkState -eq 'linked') {
            try { Remove-LinkItem $link; Write-Host "UNLINKED: $link" } catch { Write-Warning $_.Exception.Message; $ok = $false; continue }
        } elseif ($linkState -eq 'conflict') {
            Write-Warning "Foreign global install kept: $link"
        }
        $script:state.links.Remove($link)
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
            @($script:state.projects[$pending].skills) -ccontains $name)) {
            $script:state.global[$name].pending = $null
        }
    }
    foreach ($name in Get-LibraryNames) {
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
    foreach ($agent in $agentFolders) {
        if ((Get-LinkState (Get-AgentLink $Project $agent $Name) (Get-SkillTarget $Name)) -ne 'linked') { return $false }
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
    if ((Test-SamePath $project $repo) -or $project.StartsWith($repo.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar, $pathComparison)) {
        throw 'The library itself cannot be a project.'
    }
    foreach ($known in $script:state.projects.Keys) { if (Test-SamePath $known $project) { $project = $known } }
    $names = @(Read-ProjectList $project)
    if (-not $script:state.projects.ContainsKey($project)) { $script:state.projects[$project] = @{available = $true; skills = @()} }
    foreach ($name in $names) { Request-AutoOff -Name $name -Project $project }
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
    param([string]$Path, [string]$Name)
    $project = Resolve-RegisteredProject $Path
    Assert-ProjectAvailable $project
    if (-not (Test-LibrarySkill -Name $Name)) { throw "Skill is not in the library: $Name" }
    $names = @(Read-ProjectList $project)
    Request-AutoOff -Name $Name -Project $project
    Save-Bindings
    Write-ProjectList -Project $project -Names (@($names) + $Name)
    if (-not (Sync-Bindings)) { Write-Warning 'Some other bindings need attention; see status.' }
    if (-not (Test-BindingApplied $project $Name)) { Write-Warning "Binding saved but not applied: $Name in $project"; return 4 }
    return 0
}

function Invoke-Unbind {
    param([string]$Path, [string]$Name, [string]$Choice)
    if ($Choice -and $Choice -cnotin @('global', 'library')) { throw "Unknown choice: $Choice" }
    $project = Resolve-RegisteredProject $Path
    Assert-ProjectAvailable $project
    $names = @(Read-ProjectList $project)
    if ($names -ccontains $Name -and -not $Choice) {
        $others = @($script:state.projects.Keys | Where-Object {
            -not (Test-SamePath $_ $project) -and @(Get-ProjectSkills $_) -ccontains $Name })
        if (-not $others.Count) {
            # The UI asks; cancelling keeps the binding because nothing changed yet.
            $decision = [ordered]@{decision = 'last-project'; skill = $Name; project = $project
                global = (Get-GlobalChoice $Name); choices = @('global', 'library')}
            Write-Json $decision
            return 3
        }
    }
    if ($Choice) {
        Set-GlobalChoice -Name $Name -Enabled ($Choice -ceq 'global')
        Save-Bindings
    }
    Write-ProjectList -Project $project -Names @($names | Where-Object { $_ -cne $Name })
    if (-not (Sync-Bindings)) { Write-Warning 'Some other bindings need attention; see status.' }
    foreach ($agent in $agentFolders) {
        if ($script:state.links.ContainsKey((Get-AgentLink $project $agent $Name))) { return 1 }
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
        foreach ($name in $list) {
            $links = [ordered]@{}
            $stateName = 'applied'
            if (-not $available) { $stateName = 'unavailable' }
            elseif (-not (Test-LibrarySkill -Name $name -Quiet)) { $stateName = 'missing-skill' }
            else {
                foreach ($agent in $agentFolders) {
                    $link = Get-AgentLink $project $agent $name
                    $links[$link] = Get-LinkState $link (Get-SkillTarget $name)
                    if ($links[$link] -eq 'conflict') { $stateName = 'conflict' }
                    elseif ($links[$link] -eq 'missing' -and $stateName -eq 'applied') { $stateName = 'pending' }
                }
            }
            $skills += [ordered]@{name = $name; state = $stateName; links = $links}
        }
        $projects += [ordered]@{path = $project; available = $available; error = $errorValue; skills = $skills}
    }
    $rows = @()
    foreach ($name in Get-LibraryNames) {
        $choice = Get-GlobalChoice $name
        $links = [ordered]@{}
        foreach ($agent in $agentFolders) {
            $link = Get-AgentLink $HOME $agent $name
            $links[$link] = Get-LinkState $link (Get-SkillTarget $name)
        }
        $bound = @($projects | Where-Object { @($_.skills | ForEach-Object { $_.name }) -ccontains $name } | ForEach-Object { $_.path })
        $rows += [ordered]@{name = $name; projects = $bound
            global = [ordered]@{enabled = $choice.enabled; manual = $choice.manual; pending = $choice.pending; links = $links}}
    }
    $format = [Math]::Max((Get-LibraryFormat), (Get-LibraryFormat "refs/remotes/origin/$mainBranch"))
    Write-Json ([ordered]@{library = $repo; state = $bindingsFile
        format = [ordered]@{library = $format; supported = $supportedFormat; readOnly = ($format -gt $supportedFormat)}
        skills = $rows; projects = $projects})
    return 0
}
