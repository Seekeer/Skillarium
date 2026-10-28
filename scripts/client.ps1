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
                Assert-Version -Directory $old -Expected ([IO.Path]::GetFileName($old))
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
            if ($source.kind -eq 'git') {
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
