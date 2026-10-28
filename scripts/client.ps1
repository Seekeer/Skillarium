# Native PowerShell client; the Python controller is not used here.
$cache = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'agent-skills'

function Assert-SafePath {
    param([string]$RelativePath)
    if ([string]::IsNullOrEmpty($RelativePath) -or $RelativePath -match '(^/|\\|:|[\x00-\x1f])') {
        throw 'Unsafe source path.'
    }
    foreach ($part in $RelativePath.Split('/')) {
        if ($part -eq '' -or $part -eq '.' -or $part -eq '..' -or $part -match '[ .]$') {
            throw 'Unsafe source path component.'
        }
    }
}

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

function Assert-Version {
    param([string]$Directory, [string]$Expected)
    if (-not (Test-Path -LiteralPath (Join-Path $Directory 'SKILL.md') -PathType Leaf) -or
        (Get-TreeHash -Directory $Directory) -cne $Expected) {
        throw 'Skill content differs from its approved hash; existing files preserved.'
    }
}

function Set-SkillLinks {
    param([string]$Name, [string]$VersionPath)
    $linkFailed = $false
    foreach ($catalog in @((Join-Path $HOME '.agents\skills'), (Join-Path $HOME '.claude\skills'))) {
        $destination = Join-Path $catalog $Name
        $old = $null
        try {
            $existing = Get-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
            if ($existing) {
                if ($existing.LinkType -notin @('Junction', 'SymbolicLink')) {
                    throw "CONFLICT (kept): $destination"
                }
                $linkTarget = @($existing.Target)[0]
                if ([string]::IsNullOrWhiteSpace($linkTarget)) { throw 'Cannot resolve existing link.' }
                if (-not [IO.Path]::IsPathRooted($linkTarget)) { $linkTarget = Join-Path $catalog $linkTarget }
                $old = [IO.Path]::GetFullPath($linkTarget).TrimEnd('\')
                if ([string]::Equals($old, $VersionPath, [StringComparison]::OrdinalIgnoreCase)) {
                    Write-Output "OK: $destination"
                    continue
                }
                $ownedParent = Join-Path $cache "versions\$Name"
                if (-not [string]::Equals([IO.Path]::GetDirectoryName($old), $ownedParent, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "CONFLICT (kept): $destination"
                }
                try { Assert-Version -Directory $old -Expected ([IO.Path]::GetFileName($old)) }
                catch { Assert-Version -Directory $old -Expected ([IO.Path]::GetFileName($VersionPath)) }
                if ($existing.LinkType -eq 'Junction') {
                    [IO.Directory]::Delete($destination)
                } else {
                    Remove-Item -LiteralPath $destination -Force
                }
            }
            [IO.Directory]::CreateDirectory($catalog) | Out-Null
            try {
                New-Item -ItemType Junction -Path $destination -Target $VersionPath | Out-Null
            } catch {
                if ($old -and -not (Test-Path -LiteralPath $destination)) {
                    New-Item -ItemType Junction -Path $destination -Target $old | Out-Null
                }
                throw
            }
            Write-Output "LINKED: $destination"
        } catch {
            Write-Warning $_.Exception.Message
            $linkFailed = $true
        }
    }
    if ($linkFailed) { throw "Link installation incomplete: $Name" }
}

function Install-SkillEntry {
    param([string]$Name, $Entry)
    if ($Name -cnotmatch '^[a-z0-9]+(-[a-z0-9]+)*$' -or $Name.Length -gt 64 -or
        $Entry.content_sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid registry name or hash.' }
    $expected = [string]$Entry.content_sha256
    if ($Entry.customization) { $expected = [string]$Entry.customization.content_sha256 }
    if ($expected -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid shared hash.' }
    $source = $Entry.source
    $url = [string]$source.url
    if ([string]::IsNullOrEmpty($url) -or $url.StartsWith('-') -or $url -match '[\x00-\x1f]') {
        throw 'Invalid source URL.'
    }
    $versionPath = Join-Path $cache "versions\$Name\$expected"
    if (Test-Path -LiteralPath $versionPath) {
        Assert-Version -Directory $versionPath -Expected $expected
    } else {
        $temporary = Join-Path $cache ('downloads\' + [Guid]::NewGuid().ToString('N'))
        $content = Join-Path $temporary 'content'
        [IO.Directory]::CreateDirectory($content) | Out-Null
        try {
            if ($Entry.customization) {
                $shared = Join-Path $repo "customizations\$Name\$expected"
                foreach ($path in @((Join-Path $repo 'customizations'), (Join-Path $repo "customizations\$Name"), $shared)) {
                    $item = Get-Item -LiteralPath $path -Force
                    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Shared snapshot path contains a reparse point.' }
                }
                Assert-Version -Directory $shared -Expected $expected
                foreach ($item in Get-ChildItem -LiteralPath $shared -Force) {
                    Copy-Item -LiteralPath $item.FullName -Destination $content -Recurse
                }
            } elseif ($source.kind -eq 'git') {
                $revision = [string]$Entry.revision
                if ($revision -cnotmatch '^([0-9a-f]{40}|[0-9a-f]{64})$') { throw 'Invalid pinned commit.' }
                $relative = [string]$source.path
                if ([string]::IsNullOrEmpty($relative)) { $relative = '.' }
                if ($relative -ne '.') { Assert-SafePath -RelativePath $relative }
                $sha = [Security.Cryptography.SHA256]::Create()
                try { $key = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($url))).Replace('-', '').ToLowerInvariant() }
                finally { $sha.Dispose() }
                $mirror = Join-Path $cache "repositories\$key"
                if (-not (Test-Path -LiteralPath (Join-Path $mirror 'HEAD'))) {
                    [IO.Directory]::CreateDirectory($mirror) | Out-Null
                    Invoke-Git -Directory $mirror -GitArgs @('init', '--bare', '--quiet') | Out-Null
                }
                # Fetch only the approved commit; never select the source branch.
                try {
                    Invoke-Git -Directory $mirror -GitArgs @('cat-file', '-e', ($revision + '^{commit}')) | Out-Null
                } catch {
                    Invoke-Git -Directory $mirror -GitArgs @('fetch', '--quiet', '--no-tags', '--depth=1', $url, $revision) | Out-Null
                }
                $tree = $revision
                if ($relative -ne '.') { $tree += ':' + $relative }
                $listing = (Invoke-Git -Directory $mirror -GitArgs @('ls-tree', '-rz', $tree)) -join "`n"
                foreach ($row in $listing.Split([char]0)) {
                    if ($row -eq '') { continue }
                    $fields = $row.Split([char]9, 2)
                    if ($fields.Count -ne 2 -or $fields[0] -notmatch '^100(644|755) blob ') {
                        throw 'Only regular files may be imported.'
                    }
                    Assert-SafePath -RelativePath $fields[1]
                }
                $archive = Join-Path $temporary 'content.tar'
                Invoke-Git -Directory $mirror -GitArgs @('archive', '--format=tar', ('--output=' + $archive), $tree) | Out-Null
                & tar.exe -xf $archive -C $content
                if ($LASTEXITCODE -ne 0) { throw 'Cannot extract approved source.' }
            } elseif ($source.kind -eq 'url') {
                $uri = [Uri]$url
                if ($uri.Scheme -ne 'https' -or $Entry.revision -cnotmatch '^[0-9a-f]{64}$') {
                    throw 'Single-file sources require HTTPS and an approved SHA256.'
                }
                $file = Join-Path $content 'SKILL.md'
                # Windows 10 includes curl.exe; it enforces HTTPS across redirects.
                & curl.exe --proto '=https' --proto-redir '=https' --location --fail --silent --show-error --max-time 120 --max-filesize 33554432 --output $file $url
                if ($LASTEXITCODE -ne 0) { throw 'Cannot download approved file.' }
                if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Entry.revision) {
                    throw 'File differs from its approved revision.'
                }
            } else { throw 'Unsupported source kind.' }
            Assert-Version -Directory $content -Expected $expected
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($versionPath)) | Out-Null
            [IO.Directory]::Move($content, $versionPath)
        } finally {
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
        }
    }
    Set-SkillLinks -Name $Name -VersionPath $versionPath
}

function Install-Registry {
    $registry = Get-Content -LiteralPath (Join-Path $repo 'sources.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($registry.version -ne 2 -or $null -eq $registry.skills -or
        $registry.skills -isnot [PSCustomObject]) { throw 'Invalid sources.json registry.' }
    $anyFailed = $false
    foreach ($property in $registry.skills.PSObject.Properties) {
        try { Install-SkillEntry -Name $property.Name -Entry $property.Value }
        catch { Write-Warning ("ERROR {0}: {1}" -f $property.Name, $_.Exception.Message); $anyFailed = $true }
    }
    if ($anyFailed) { throw 'Some approved skills could not be installed.' }
}

function Publish-LocalChanges {
    $changes = @(Invoke-Git -GitArgs @('status', '--porcelain', '--untracked-files=all'))
    if ($changes.Count) { throw 'Repository has local changes; sharing skipped.' }
    $gitDir = Invoke-Git -GitArgs @('rev-parse', '--absolute-git-dir')
    $temporary = Join-Path $gitDir ('shared-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temporary) | Out-Null
    try {
        $registry = Get-Content -LiteralPath (Join-Path $repo 'sources.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $snapshots = @()
        foreach ($property in $registry.skills.PSObject.Properties) {
            $name = $property.Name
            $entry = $property.Value
            $expected = [string]$entry.content_sha256
            if ($entry.customization) { $expected = [string]$entry.customization.content_sha256 }
            if ($name -cnotmatch '^[a-z0-9]+(-[a-z0-9]+)*$' -or $name.Length -gt 64 -or
                $expected -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid registry name or hash.' }
            $selected = $null
            foreach ($catalog in @((Join-Path $HOME '.agents\skills'), (Join-Path $HOME '.claude\skills'))) {
                $link = Get-Item -LiteralPath (Join-Path $catalog $name) -Force -ErrorAction SilentlyContinue
                if (-not $link -or $link.LinkType -notin @('Junction', 'SymbolicLink')) { continue }
                $old = [string]@($link.Target)[0]
                if (-not [IO.Path]::IsPathRooted($old)) { $old = Join-Path $catalog $old }
                $old = [IO.Path]::GetFullPath($old).TrimEnd('\')
                if (-not [string]::Equals([IO.Path]::GetDirectoryName($old), (Join-Path $cache "versions\$name"), [StringComparison]::OrdinalIgnoreCase)) { continue }
                $oldHash = [IO.Path]::GetFileName($old)
                if ($oldHash -cnotmatch '^[0-9a-f]{64}$' -or -not (Test-Path -LiteralPath $old)) { continue }
                $actual = Get-TreeHash -Directory $old
                if ($actual -ceq $oldHash -or $actual -ceq $expected) { continue }
                if ($oldHash -cne $expected -or ($selected -and $selected -cne $actual)) {
                    throw "SHARED CONFLICT (kept): $name; another version was published"
                }
                if (-not (Test-Path -LiteralPath (Join-Path $old 'SKILL.md') -PathType Leaf)) { throw "Missing SKILL.md (kept): $name" }
                $selected = $actual
                $destination = Join-Path $temporary "customizations\$name\$actual"
                if (-not (Test-Path -LiteralPath $destination)) {
                    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
                    Copy-Item -LiteralPath $old -Destination $destination -Recurse
                }
                if ((Get-TreeHash -Directory $destination) -cne $actual -or (Get-TreeHash -Directory $old) -cne $actual) {
                    throw 'Skill changed during snapshot; preserved.'
                }
                foreach ($file in Get-ChildItem -LiteralPath $destination -Force -Recurse) {
                    $relative = $file.FullName.Substring($destination.Length + 1).Replace('\', '/')
                    Assert-SafePath -RelativePath $relative
                    if ($relative -match '(^|/)(\.git|\.gitattributes)(/|$)') { throw 'Git metadata in shared snapshots is unsupported.' }
                }
            }
            if ($selected) {
                $entry | Add-Member -NotePropertyName customization -NotePropertyValue ([PSCustomObject]@{
                    content_sha256 = $selected; base_sha256 = $entry.content_sha256; base_revision = $entry.revision
                }) -Force
                $snapshots += "customizations\$name\$selected"
            }
        }
        if (-not $snapshots.Count) { return }
        $sharedRoot = Join-Path $repo 'customizations'
        if (Test-Path -LiteralPath $sharedRoot) {
            $items = @(Get-Item -LiteralPath $sharedRoot -Force) + @(Get-ChildItem -LiteralPath $sharedRoot -Force -Recurse)
            foreach ($item in $items) {
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Shared snapshot path contains a reparse point.' }
            }
        }
        foreach ($relative in $snapshots) {
            $destination = Join-Path $repo $relative
            if (Test-Path -LiteralPath $destination) {
                Assert-Version -Directory $destination -Expected ([IO.Path]::GetFileName($destination))
            } else {
                [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
                Copy-Item -LiteralPath (Join-Path $temporary $relative) -Destination $destination -Recurse
            }
        }
        $json = ($registry | ConvertTo-Json -Depth 30) + "`n"
        [IO.File]::WriteAllText((Join-Path $repo 'sources.json'), $json, (New-Object Text.UTF8Encoding($false)))
        Invoke-Git -GitArgs @('add', '--', 'sources.json', 'customizations') | Out-Null
        Invoke-Git -GitArgs @('-c', 'user.name=agent-skills', '-c', 'user.email=agent-skills@localhost',
            'commit', '-m', "Share local skill customizations`n`nAgent-Skills-Client: 1", '--', 'sources.json', 'customizations') | Out-Null
        Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'push') | Out-Null
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
    }
}
