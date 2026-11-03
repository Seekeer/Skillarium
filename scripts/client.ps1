# Git-only PowerShell client. The server owns all external-source operations.
$cache = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'agent-skills'

function Get-Registry {
    $registry = Get-Content -LiteralPath (Join-Path $repo 'sources.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($registry.version -ne 2 -or $registry.skills -isnot [PSCustomObject]) { throw 'Invalid skill registry.' }
    return $registry
}

function Assert-WindowsPaths {
    param([string]$Name)
    $seen = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::OrdinalIgnoreCase)
    $files = (Invoke-Git -GitArgs @('ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', "skills/$Name")) -join "`n"
    foreach ($path in @("skills/$Name") + @($files.Split([char]0))) {
        if (-not $path -or -not (Test-Path -LiteralPath (Join-Path $repo $path))) { continue }
        $prefix = ''
        foreach ($part in $path.Split('/')) {
            $stem = $part.Split('.')[0].TrimEnd(' ')
            if ($part -match '[<>:"\\|?*\x00-\x1f]' -or $part.EndsWith(' ') -or $part.EndsWith('.') -or
                $part.Length -gt 255 -or $part -in @('.git', '.gitattributes') -or
                $stem -match '^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(COM|LPT)[1-9\u00b9\u00b2\u00b3])$') {
                throw "Windows-incompatible skill path: $path"
            }
            if ($prefix) { $prefix += '/' }
            $prefix += $part
            if ($seen.ContainsKey($prefix) -and $seen[$prefix] -cne $prefix) {
                throw "Windows path collision: $($seen[$prefix]) / $prefix"
            }
            $seen[$prefix] = $prefix
        }
    }
}

function Assert-CatalogSkill {
    param([string]$Name)
    if ($Name -cnotmatch '^[a-z0-9]+(-[a-z0-9]+)*$' -or $Name.Length -gt 64) { throw 'Invalid skill name.' }
    $folder = Join-Path $repo "skills\$Name"
    foreach ($path in @((Join-Path $repo 'skills'), $folder, (Join-Path $folder 'SKILL.md'))) {
        $item = Get-Item -LiteralPath $path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unsafe catalog skill path.' }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $folder 'SKILL.md') -PathType Leaf)) { throw 'Missing SKILL.md; run server maintenance.' }
    foreach ($item in Get-ChildItem -LiteralPath $folder -Force -Recurse) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Name -in @('.git', '.gitattributes')) {
            throw 'Unsupported link or Git metadata in a skill; preserved.'
        }
    }
    Assert-WindowsPaths -Name $Name
}

# Used only to recognize and preserve old cache installations during migration.
function Add-TreeHash {
    param([string]$Directory, [string]$Prefix, [IO.Stream]$Stream)
    # UTF-8 byte order matches Python pathlib's component-by-component sort.
    $items = @{}
    foreach ($item in Get-ChildItem -LiteralPath $Directory -Force) {
        $key = [BitConverter]::ToString([Text.Encoding]::UTF8.GetBytes($item.Name)).Replace('-', '')
        $items[$key] = $item
    }
    [string[]]$keys = @($items.Keys)
    [Array]::Sort($keys, [StringComparer]::Ordinal)
    foreach ($key in $keys) {
        $item = $items[$key]
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Reparse points in skill content are unsupported.'
        }
        $relative = $Prefix + $item.Name
        if ($item.PSIsContainer) {
            Add-TreeHash -Directory $item.FullName -Prefix ($relative + '/') -Stream $Stream
        } else {
            $bytes = [Text.Encoding]::UTF8.GetBytes($relative)
            $Stream.Write($bytes, 0, $bytes.Length)
            $Stream.WriteByte(0)
            $file = [IO.File]::OpenRead($item.FullName)
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $digest = $sha.ComputeHash($file) } finally { $sha.Dispose(); $file.Dispose() }
            $Stream.Write($digest, 0, $digest.Length)
            $Stream.WriteByte(0)
        }
    }
}

function Get-TreeHash {
    param([string]$Directory)
    $stream = New-Object IO.MemoryStream
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        Add-TreeHash -Directory $Directory -Prefix '' -Stream $stream
        $stream.Position = 0
        return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose(); $stream.Dispose() }
}

function Migrate-LegacyLinks {
    $registry = Get-Registry
    foreach ($property in $registry.skills.PSObject.Properties) {
        $name = $property.Name
        $folder = Join-Path $repo "skills\$name"
        if (-not (Test-Path -LiteralPath $folder)) { continue }
        Assert-CatalogSkill -Name $name
        foreach ($catalog in @((Join-Path $HOME '.agents\skills'), (Join-Path $HOME '.claude\skills'))) {
            $link = Get-Item -LiteralPath (Join-Path $catalog $name) -Force -ErrorAction SilentlyContinue
            if (-not $link -or $link.LinkType -notin @('Junction', 'SymbolicLink')) { continue }
            $old = [string]@($link.Target)[0]
            if (-not [IO.Path]::IsPathRooted($old)) { $old = Join-Path $catalog $old }
            $old = [IO.Path]::GetFullPath($old).TrimEnd('\')
            if (-not [string]::Equals([IO.Path]::GetDirectoryName($old), (Join-Path $cache "versions\$name"), [StringComparison]::OrdinalIgnoreCase)) { continue }
            $baseline = [IO.Path]::GetFileName($old)
            if ($baseline -cnotmatch '^[0-9a-f]{64}$') { continue }
            $actual = Get-TreeHash -Directory $old
            if ($actual -ceq $baseline) { continue }
            if (-not (Test-Path -LiteralPath (Join-Path $old 'SKILL.md') -PathType Leaf)) { throw "Legacy deletion preserved: $name" }
            $expected = $property.Value.content_sha256
            if ($property.Value.customization) { $expected = $property.Value.customization.content_sha256 }
            $current = Get-TreeHash -Directory $folder
            if ($current -ceq $actual) { Set-SkillLinks -Name $name; continue }
            if ($expected -cne $baseline -or $current -cne $baseline) { throw "CONFLICT: legacy edits differ from catalog; preserved: $old" }
            $temporary = Join-Path (Invoke-Git -GitArgs @('rev-parse', '--absolute-git-dir')) ('legacy-' + [Guid]::NewGuid().ToString('N'))
            try {
                Copy-Item -LiteralPath $old -Destination $temporary -Recurse
                if ((Get-TreeHash -Directory $temporary) -cne $actual -or (Get-TreeHash -Directory $old) -cne $actual) { throw 'Legacy skill changed during migration.' }
                Remove-Item -LiteralPath $folder -Recurse -Force
                Move-Item -LiteralPath $temporary -Destination $folder
                Set-SkillLinks -Name $name
            } finally {
                if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
            }
        }
    }
}

function Publish-LocalChanges {
    $registry = Get-Registry
    if (@(Invoke-Git -GitArgs @('diff', '--cached', '--name-only')).Count) { throw 'Staged changes preserved.' }
    $status = (Invoke-Git -GitArgs @('-c', 'status.renames=false', 'status', '--porcelain', '--untracked-files=all', '-z')) -join "`n"
    if (-not $status) { return }
    $paths = @()
    foreach ($row in $status.Split([char]0)) {
        if (-not $row) { continue }
        $path = $row.Substring(3)
        if ($path -cnotmatch '^skills/([a-z0-9]+(?:-[a-z0-9]+)*)/') { throw "Unrelated local changes preserved: $path" }
        $name = $Matches[1]
        if (-not $registry.skills.PSObject.Properties[$name]) { throw "Unregistered local skill preserved: $name" }
        Assert-CatalogSkill -Name $name
        $paths += ':(literal)' + $path
    }
    Invoke-Git -GitArgs (@('add', '-A', '--') + $paths) | Out-Null
    Invoke-Git -GitArgs @('-c', 'user.name=agent-skills', '-c', 'user.email=agent-skills@localhost',
        'commit', '-m', "Share local skill edits`n`nAgent-Skills-Client: 1") | Out-Null
}

function Assert-AutomaticCommits {
    foreach ($revision in @(Invoke-Git -GitArgs @('rev-list', '@{upstream}..HEAD'))) {
        $message = (Invoke-Git -GitArgs @('show', '-s', '--format=%B', $revision)) -join "`n"
        if ($message -notmatch '(?m)^Agent-Skills-Client: 1$') { throw 'Unpublished manual commits preserved.' }
    }
}

function Reconcile-Repository {
    & git -C $repo merge-base --is-ancestor '@{upstream}' HEAD
    if ($LASTEXITCODE -eq 0) { return }
    & git -C $repo merge-base --is-ancestor HEAD '@{upstream}'
    if ($LASTEXITCODE -eq 0) {
        Invoke-Git -GitArgs @('merge', '--ff-only', '--no-overwrite-ignore', '@{upstream}') | Out-Null
        return
    }
    $temporary = Join-Path (Invoke-Git -GitArgs @('rev-parse', '--absolute-git-dir')) ('client-merge-' + [Guid]::NewGuid().ToString('N'))
    Invoke-Git -GitArgs @('worktree', 'add', '--detach', $temporary, 'HEAD') | Out-Null
    try {
        $upstream = Invoke-Git -GitArgs @('rev-parse', '@{upstream}')
        # Use an empty hooks directory on both Windows and Linux.
        $hooks = Join-Path $temporary '.empty-hooks'
        [IO.Directory]::CreateDirectory($hooks) | Out-Null
        try {
            Invoke-Git -Directory $temporary -GitArgs @('-c', 'user.name=agent-skills', '-c', 'user.email=agent-skills@localhost',
                '-c', "core.hooksPath=$hooks", 'merge', '--no-ff', '--no-edit', '-m',
                "Reconcile shared skill edits`n`nAgent-Skills-Client: 1", $upstream) | Out-Null
        } catch { throw 'CONFLICT: local commit preserved; installed files unchanged. Resolve Git history manually.' }
        $merged = Invoke-Git -Directory $temporary -GitArgs @('rev-parse', 'HEAD')
        Invoke-Git -GitArgs @('merge', '--ff-only', '--no-overwrite-ignore', $merged) | Out-Null
    } finally { Invoke-Git -GitArgs @('worktree', 'remove', '--force', $temporary) | Out-Null }
}

function Sync-Repository {
    $remotes = @(Invoke-Git -GitArgs @('remote'))
    if ($remotes -notcontains 'origin') { Write-Warning 'No origin; linking local catalog only.'; return }
    Invoke-Git -GitArgs @('symbolic-ref', '-q', 'HEAD') | Out-Null
    Invoke-Git -GitArgs @('rev-parse', '--verify', '@{upstream}') | Out-Null
    Assert-AutomaticCommits
    Migrate-LegacyLinks
    Publish-LocalChanges
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'fetch', 'origin') | Out-Null
        Assert-AutomaticCommits
        Reconcile-Repository
        Migrate-LegacyLinks
        Publish-LocalChanges
        if (-not @(Invoke-Git -GitArgs @('rev-list', '@{upstream}..HEAD')).Count) { return }
        try { Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'push') | Out-Null; return }
        catch { if ($attempt -eq 1) { throw 'Git publication failed; local commit preserved for retry.' } }
    }
}

function New-SkillLink {
    param([string]$Path, [string]$Target)
    # Junctions need no privileges on Windows; PowerShell on Linux uses symlinks.
    $type = 'SymbolicLink'
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) { $type = 'Junction' }
    New-Item -ItemType $type -Path $Path -Target $Target | Out-Null
}

function Set-SkillLinks {
    param([string]$Name)
    Assert-CatalogSkill -Name $Name
    $target = Join-Path $repo "skills\$Name"
    $failed = $false
    foreach ($catalog in @((Join-Path $HOME '.agents\skills'), (Join-Path $HOME '.claude\skills'))) {
        $destination = Join-Path $catalog $Name
        $old = $null
        try {
            $link = Get-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
            if ($link) {
                if ($link.LinkType -notin @('Junction', 'SymbolicLink')) { throw "CONFLICT (kept): $destination" }
                $old = [string]@($link.Target)[0]
                if (-not [IO.Path]::IsPathRooted($old)) { $old = Join-Path $catalog $old }
                $old = [IO.Path]::GetFullPath($old).TrimEnd('\')
                if ([string]::Equals($old, $target, [StringComparison]::OrdinalIgnoreCase)) { continue }
                $baseline = [IO.Path]::GetFileName($old)
                if (-not [string]::Equals([IO.Path]::GetDirectoryName($old), (Join-Path $cache "versions\$Name"), [StringComparison]::OrdinalIgnoreCase) -or $baseline -cnotmatch '^[0-9a-f]{64}$') { throw "CONFLICT (kept): $destination" }
                $actual = Get-TreeHash -Directory $old
                if ($actual -cne $baseline -and $actual -cne (Get-TreeHash -Directory $target)) { throw "Legacy edits preserved: $destination" }
                if ($link.LinkType -eq 'Junction') { [IO.Directory]::Delete($destination) }
                else { Remove-Item -LiteralPath $destination -Force }
            }
            [IO.Directory]::CreateDirectory($catalog) | Out-Null
            try { New-SkillLink -Path $destination -Target $target }
            catch {
                if ($old -and -not (Test-Path -LiteralPath $destination)) { New-SkillLink -Path $destination -Target $old }
                throw
            }
        } catch { Write-Warning $_.Exception.Message; $failed = $true }
    }
    if ($failed) { throw "Link installation incomplete: $Name" }
}

# Conflicts that stop Reconcile-Repository, shown and resolved by the "Projects" UI.
function Get-GitBlob {
    param([string]$Id)
    if ($Id -cnotmatch '^[0-9a-f]{40}([0-9a-f]{24})?$') { throw 'Invalid Git object id.' }
    $info = New-Object Diagnostics.ProcessStartInfo 'git'
    $info.Arguments = "cat-file blob $Id"
    $info.WorkingDirectory = $repo
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $process = [Diagnostics.Process]::Start($info)
    $buffer = New-Object IO.MemoryStream
    try {
        $process.StandardOutput.BaseStream.CopyTo($buffer)
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "git failed (exit $($process.ExitCode))." }
        return ,$buffer.ToArray()
    } finally { $buffer.Dispose(); $process.Dispose() }
}

function Get-GitObjectId {
    param([string]$Revision, [string]$Path)
    $id = & git -C $repo rev-parse --verify --quiet "${Revision}:$Path"
    if ($LASTEXITCODE -ne 0) { return $null }
    return [string]$id
}

function ConvertTo-ConflictSide {
    param([byte[]]$Bytes, [string]$Id)
    $text = $null
    if ([Array]::IndexOf($Bytes, [byte]0) -lt 0) {
        try { $text = (New-Object Text.UTF8Encoding($false, $true)).GetString($Bytes) } catch { $text = $null }
    }
    return [ordered]@{id = $Id; binary = ($null -eq $text); text = $text}
}

function Get-DivergedRevisions {
    if (@(Invoke-Git -GitArgs @('remote')) -notcontains 'origin') { return $null }
    & git -C $repo symbolic-ref -q HEAD | Out-Null
    if ($LASTEXITCODE -ne 0) { return $null }
    $upstream = & git -C $repo rev-parse --verify --quiet '@{upstream}'
    if ($LASTEXITCODE -ne 0) { return $null }
    $head = Invoke-Git -GitArgs @('rev-parse', 'HEAD')
    foreach ($pair in @(@($upstream, $head), @($head, $upstream))) {
        & git -C $repo merge-base --is-ancestor $pair[0] $pair[1]
        if ($LASTEXITCODE -eq 0) { return $null }
    }
    return @([string]$head, [string]$upstream)
}

# Repeats the background merge in a disposable worktree and passes its unmerged paths to $Then.
function Invoke-TrialMerge {
    param([string]$Head, [string]$Upstream, [scriptblock]$Then)
    $temporary = Join-Path (Invoke-Git -GitArgs @('rev-parse', '--absolute-git-dir')) ('client-merge-' + [Guid]::NewGuid().ToString('N'))
    # No stderr redirection: Windows PowerShell turns redirected native stderr into errors.
    & git -C $repo worktree add --quiet --detach $temporary $Head | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare a trial merge.' }
    try {
        $hooks = Join-Path $temporary '.empty-hooks'
        [IO.Directory]::CreateDirectory($hooks) | Out-Null
        & git -C $temporary -c user.name=agent-skills -c user.email=agent-skills@localhost -c "core.hooksPath=$hooks" merge --quiet --no-commit --no-ff $Upstream | Out-Null
        $merged = $LASTEXITCODE -eq 0
        $paths = @(((& git -C $temporary diff --name-only -z --diff-filter=U) -join "`n").Split([char]0) | Where-Object { $_ })
        if (-not $merged -and -not $paths.Count) { throw 'Trial merge failed without conflicts.' }
        return & $Then $temporary $hooks $paths
    } finally { Invoke-Git -GitArgs @('worktree', 'remove', '--force', $temporary) | Out-Null }
}

# The local side is the working tree: the next synchronization commits it first.
function Invoke-Conflicts {
    $revisions = Get-DivergedRevisions
    $files = @()
    if ($revisions) {
        $paths = Invoke-TrialMerge -Head $revisions[0] -Upstream $revisions[1] -Then { param($worktree, $hooks, $paths) return ,$paths }
        foreach ($path in $paths) {
            if ($path -cnotmatch '^skills/([a-z0-9]+(?:-[a-z0-9]+)*)/') { throw "Conflict outside skills/ needs manual Git resolution: $path" }
            $skill = $Matches[1]
            $mine = $null
            $full = Join-Path $repo $path
            if (Test-Path -LiteralPath $full -PathType Leaf) {
                $id = [string](Invoke-Git -GitArgs @('hash-object', '--no-filters', '--', $path))
                $mine = ConvertTo-ConflictSide -Bytes ([IO.File]::ReadAllBytes($full)) -Id $id
            }
            $library = $null
            $id = Get-GitObjectId -Revision $revisions[1] -Path $path
            if ($id) { $library = ConvertTo-ConflictSide -Bytes (Get-GitBlob -Id $id) -Id $id }
            $files += [ordered]@{path = $path; skill = $skill; mine = $mine; library = $library}
        }
    }
    $state = 'ok'
    if ($files.Count) { $state = 'conflict' }
    $upstream = $null
    if ($revisions) { $upstream = $revisions[1] }
    Write-Json ([ordered]@{state = $state; upstream = $upstream; files = $files})
    return 0
}

# Request: {upstream, files: [{path, take: mine|library|edited, mine, library, text}]}, where mine and
# library are the object ids that were shown. Exit 5: the conflict changed and nothing was merged.
function Invoke-Resolve {
    param([string]$RequestPath)
    $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'Never'
    if (-not $env:GIT_SSH_COMMAND) { $env:GIT_SSH_COMMAND = 'ssh -oBatchMode=yes -oConnectTimeout=15' }
    Assert-AutomaticCommits
    Publish-LocalChanges
    $revisions = Get-DivergedRevisions
    if (-not $revisions -or $revisions[1] -cne [string]$request.upstream) { Write-Warning 'The library changed; review the conflict again.'; return 5 }
    $choices = @{}
    foreach ($file in @($request.files)) { $choices[[string]$file.path] = $file }
    $merged = Invoke-TrialMerge -Head $revisions[0] -Upstream $revisions[1] -Then {
        param($worktree, $hooks, $paths)
        if ((@($paths | Sort-Object -CaseSensitive) -join "`n") -cne (@($choices.Keys | Sort-Object -CaseSensitive) -join "`n")) { return $null }
        foreach ($path in $paths) {
            $choice = $choices[$path]
            $mineId = Get-GitObjectId -Revision $revisions[0] -Path $path
            $libraryId = Get-GitObjectId -Revision $revisions[1] -Path $path
            if ([string]$choice.mine -cne [string]$mineId -or [string]$choice.library -cne [string]$libraryId) { return $null }
            $bytes = $null
            switch -CaseSensitive ([string]$choice.take) {
                'mine' { if ($mineId) { $bytes = Get-GitBlob -Id $mineId } }
                'library' { if ($libraryId) { $bytes = Get-GitBlob -Id $libraryId } }
                'edited' {
                    if ($null -eq $choice.text) { throw "No edited text: $path" }
                    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes([string]$choice.text)
                }
                default { throw "Unknown choice for $path." }
            }
            $target = Join-Path $worktree $path
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
            if ($null -ne $bytes) {
                [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
                [IO.File]::WriteAllBytes($target, $bytes)
            }
            Invoke-Git -Directory $worktree -GitArgs @('add', '-A', '--', ":(literal)$path") | Out-Null
        }
        if (@(& git -C $worktree diff --name-only --diff-filter=U).Count) { throw 'Unresolved files remain; nothing merged.' }
        Invoke-Git -Directory $worktree -GitArgs @('-c', 'user.name=agent-skills', '-c', 'user.email=agent-skills@localhost',
            '-c', "core.hooksPath=$hooks", 'commit', '--no-edit', '-m', "Resolve shared skill edits`n`nAgent-Skills-Client: 1") | Out-Null
        return [string](Invoke-Git -Directory $worktree -GitArgs @('rev-parse', 'HEAD'))
    }
    if (-not $merged) { Write-Warning 'The conflict changed; review it again.'; return 5 }
    Invoke-Git -GitArgs @('merge', '--ff-only', '--no-overwrite-ignore', $merged) | Out-Null
    $pushed = $true
    & git -C $repo -c credential.interactive=false push --quiet | Out-Null
    if ($LASTEXITCODE -ne 0) { $pushed = $false; Write-Warning 'Publication failed; the next synchronization retries it.' }
    if (-not (Sync-Bindings)) { Write-Warning 'Some skill links need attention; see status.' }
    Write-Json ([ordered]@{resolved = $true; pushed = $pushed})
    return 0
}
