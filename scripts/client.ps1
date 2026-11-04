# Git-only PowerShell client. The server owns all external-source operations.
# Publications go through proposal branches and pull requests; main is never pushed.
$cacheRoot = $env:LOCALAPPDATA
if (-not $cacheRoot) { $cacheRoot = [Environment]::GetFolderPath('LocalApplicationData') }
$cache = Join-Path $cacheRoot 'agent-skills'
$mainBranch = 'main'
$proposalPrefix = 'agent-skills/update'

function Get-Registry {
    $registry = Get-Content -LiteralPath (Join-Path $repo 'sources.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($registry.version -ne 2 -or $registry.skills -isnot [PSCustomObject]) { throw 'Invalid skill registry.' }
    return $registry
}

function Invoke-GitCode {
    param([string[]]$GitArgs, [string]$Directory = $repo)
    & git -C $Directory @GitArgs 2>$null | Out-Null
    return $LASTEXITCODE
}

# Registered skills plus folders published by clients before server registration.
function Get-LibraryNames {
    $names = New-Object 'Collections.Generic.SortedSet[string]' ([StringComparer]::Ordinal)
    foreach ($property in (Get-Registry).skills.PSObject.Properties) { [void]$names.Add($property.Name) }
    $root = Join-Path $repo 'skills'
    if (Test-Path -LiteralPath $root) {
        foreach ($item in Get-ChildItem -LiteralPath $root -Directory -Force) {
            if ($item.Name -cmatch '^[a-z0-9]+(-[a-z0-9]+)*$') { [void]$names.Add($item.Name) }
        }
    }
    return @($names)
}

# Directories receiving links; project bindings may add further roots.
function Get-LinkRoots {
    return @((Join-Path $HOME '.agents\skills'), (Join-Path $HOME '.claude\skills'))
}

function Get-MachineName {
    $name = (& git -C $repo config --get agent-skills.machine)
    if ($LASTEXITCODE -ne 0 -or -not $name) { $name = $env:COMPUTERNAME }
    if ($name -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$') {
        throw 'Invalid machine name; set: git config agent-skills.machine <name>'
    }
    return [string]$name
}

function Get-CloneId {
    $id = (& git -C $repo config --get agent-skills.clone-id)
    if ($LASTEXITCODE -eq 0 -and $id) { return [string]$id }
    $id = [Guid]::NewGuid().ToString('N').Substring(0, 8)
    Invoke-Git -GitArgs @('config', 'agent-skills.clone-id', $id) | Out-Null
    return $id
}

# Service identity per command; the user's own Git settings stay untouched.
function Invoke-ServiceGit {
    param([string[]]$GitArgs, [string]$Directory = $repo)
    $machine = Get-MachineName
    $saved = @{}
    $values = @{
        GIT_AUTHOR_NAME = "agent-skills/$machine"; GIT_COMMITTER_NAME = "agent-skills/$machine"
        GIT_AUTHOR_EMAIL = "agent-skills+$machine@localhost"; GIT_COMMITTER_EMAIL = "agent-skills+$machine@localhost"
    }
    foreach ($key in $values.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $values[$key], 'Process')
    }
    try { return Invoke-Git -Directory $Directory -GitArgs $GitArgs }
    finally { foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process') } }
}

function Assert-WindowsPaths {
    param([string]$Name, [string]$Root = $repo)
    $seen = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::OrdinalIgnoreCase)
    $files = (Invoke-Git -Directory $Root -GitArgs @('ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', "skills/$Name")) -join "`n"
    foreach ($path in @("skills/$Name") + @($files.Split([char]0))) {
        if (-not $path -or -not (Test-Path -LiteralPath (Join-Path $Root $path))) { continue }
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

# Frontmatter structure only; the meaning of the instructions is not reviewed.
function Assert-SkillText {
    param([string]$Name, [string]$Path)
    $lines = [IO.File]::ReadAllText($Path, (New-Object Text.UTF8Encoding($false))).TrimStart([char]0xFEFF) -split "`r?`n"
    $end = -1
    if ($lines[0] -ceq '---') {
        for ($index = 1; $index -lt $lines.Count; $index++) { if ($lines[$index] -ceq '---') { $end = $index; break } }
    }
    if ($end -lt 0) { throw "Invalid skill ${Name}: SKILL.md needs frontmatter" }
    $header = $lines[1..($end - 1)]
    $found = $header | Where-Object { $_ -match '^name:\s*(.+)$' } | Select-Object -First 1
    if (-not $found -or ($found -replace '^name:\s*', '').Trim().Trim('"', "'") -cne $Name) {
        throw "Invalid skill ${Name}: SKILL.md name must match the folder"
    }
    if (-not ($header | Where-Object { $_ -match '^description:\s*\S' })) { throw "Invalid skill ${Name}: SKILL.md needs a description" }
}

function Assert-CatalogSkill {
    param([string]$Name, [string]$Root = $repo)
    if ($Name -cnotmatch '^[a-z0-9]+(-[a-z0-9]+)*$' -or $Name.Length -gt 64) { throw "Invalid skill name: $Name" }
    $folder = Join-Path $Root "skills\$Name"
    if (-not (Test-Path -LiteralPath (Join-Path $folder 'SKILL.md') -PathType Leaf)) {
        throw "Missing skill folder or SKILL.md: $Name; deleting a whole skill needs an explicit management change."
    }
    foreach ($path in @((Join-Path $Root 'skills'), $folder, (Join-Path $folder 'SKILL.md'))) {
        $item = Get-Item -LiteralPath $path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unsafe catalog skill path.' }
    }
    foreach ($item in Get-ChildItem -LiteralPath $folder -Force -Recurse) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Name -in @('.git', '.gitattributes')) {
            throw 'Unsupported link or Git metadata in a skill; preserved.'
        }
    }
    Assert-SkillText -Name $Name -Path (Join-Path $folder 'SKILL.md')
    Assert-WindowsPaths -Name $Name -Root $Root
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

# Any valid edit of a skill folder, including a new own skill, is published.
function Publish-LocalChanges {
    Get-Registry | Out-Null
    if (@(Invoke-Git -GitArgs @('diff', '--cached', '--name-only')).Count) { throw 'Staged changes preserved.' }
    $status = (Invoke-Git -GitArgs @('-c', 'status.renames=false', 'status', '--porcelain', '--untracked-files=all', '-z')) -join "`n"
    if (-not $status) { return }
    $paths = @()
    $checked = @{}
    foreach ($row in $status.Split([char]0)) {
        if (-not $row) { continue }
        $path = $row.Substring(3)
        if ($path -cnotmatch '^skills/([^/]+)/') { throw "Unrelated local changes preserved: $path" }
        $name = $Matches[1]
        if (-not $checked.ContainsKey($name)) { Assert-CatalogSkill -Name $name; $checked[$name] = $true }
        $paths += ':(literal)' + $path
    }
    Invoke-Git -GitArgs (@('add', '-A', '--') + $paths) | Out-Null
    Invoke-ServiceGit -GitArgs @('commit', '-q', '-m', "Share local skill edits`n`nAgent-Skills-Client: 1") | Out-Null
}

function Assert-AutomaticCommits {
    foreach ($revision in @(Invoke-Git -GitArgs @('rev-list', '@{upstream}..HEAD'))) {
        $message = (Invoke-Git -GitArgs @('show', '-s', '--format=%B', $revision)) -join "`n"
        if ($message -notmatch '(?m)^Agent-Skills-Client: 1$') { throw 'Unpublished manual commits preserved.' }
    }
}

function Reconcile-Repository {
    if ((Invoke-GitCode -GitArgs @('merge-base', '--is-ancestor', '@{upstream}', 'HEAD')) -eq 0) { return }
    if ((Invoke-GitCode -GitArgs @('merge-base', '--is-ancestor', 'HEAD', '@{upstream}')) -eq 0) {
        Invoke-Git -GitArgs @('merge', '-q', '--ff-only', '--no-overwrite-ignore', '@{upstream}') | Out-Null
        return
    }
    $temporary = Join-Path (Invoke-Git -GitArgs @('rev-parse', '--absolute-git-dir')) ('client-merge-' + [Guid]::NewGuid().ToString('N'))
    Invoke-Git -GitArgs @('worktree', 'add', '-q', '--detach', $temporary, 'HEAD') | Out-Null
    try {
        $upstream = Invoke-Git -GitArgs @('rev-parse', '@{upstream}')
        # Use an empty hooks directory on both Windows and Linux.
        $hooks = Join-Path $temporary '.empty-hooks'
        [IO.Directory]::CreateDirectory($hooks) | Out-Null
        try {
            Invoke-ServiceGit -Directory $temporary -GitArgs @('-c', "core.hooksPath=$hooks", 'merge', '-q', '--no-ff', '--no-edit', '-m',
                "Reconcile shared skill edits`n`nAgent-Skills-Client: 1", $upstream) | Out-Null
        } catch { throw 'CONFLICT: local commit preserved; installed files unchanged. Resolve Git history manually.' }
        $merged = Invoke-Git -Directory $temporary -GitArgs @('rev-parse', 'HEAD')
        Invoke-Git -GitArgs @('merge', '-q', '--ff-only', '--no-overwrite-ignore', $merged) | Out-Null
    } finally { Invoke-Git -GitArgs @('worktree', 'remove', '--force', $temporary) | Out-Null }
}

# --- Hosting API -------------------------------------------------------------

function Get-ApiBase {
    $configured = (& git -C $repo config --get agent-skills.api)
    if ($LASTEXITCODE -eq 0 -and $configured) { return ([string]$configured).TrimEnd('/') }
    $url = [string](Invoke-Git -GitArgs @('remote', 'get-url', 'origin'))
    if ($url -match '^(https?)://(?:[^@/]*@)?([^/]+)/(.+?)(?:\.git)?/?$') { return "$($Matches[1])://$($Matches[2])/api/v1/repos/$($Matches[3])" }
    if ($url -match '^(?:ssh://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$') { return "https://$($Matches[1])/api/v1/repos/$($Matches[2])" }
    throw 'Cannot derive hosting API; set git config agent-skills.api'
}

$script:apiAuthorization = $null

# Credentials come from the Git credential helper; errors never include them.
function Invoke-Api {
    param([string]$Method, [string]$Path, $Body = $null)
    $base = Get-ApiBase
    if (-not $script:apiAuthorization) {
        $uri = [Uri]$base
        $authority = $uri.Authority
        $request = "protocol=$($uri.Scheme)`nhost=$authority`n`n"
        # A console input encoding with a BOM (Windows "UTF-8 for worldwide language support")
        # prefixes piped input with it, and Git then sees no protocol field.
        $inputEncoding = [Console]::InputEncoding
        try {
            [Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
            $output = $request | & git -C $repo -c credential.interactive=false credential fill 2>$null
        } finally { [Console]::InputEncoding = $inputEncoding }
        $values = @{}
        foreach ($line in @($output)) { if ($line -match '^([^=]+)=(.*)$') { $values[$Matches[1]] = $Matches[2] } }
        if ($LASTEXITCODE -ne 0 -or -not $values['password']) { throw 'Hosting credentials are unavailable; publication pending.' }
        $pair = [Text.Encoding]::UTF8.GetBytes("$($values['username']):$($values['password'])")
        $script:apiAuthorization = 'Basic ' + [Convert]::ToBase64String($pair)
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $web = [Net.HttpWebRequest]::Create($base + $Path)
    $web.Method = $Method
    $web.Timeout = 30000
    $web.Accept = 'application/json'
    $web.Headers['Authorization'] = $script:apiAuthorization
    if ($null -ne $Body) {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 6 -Compress))
        $web.ContentType = 'application/json'
        $stream = $web.GetRequestStream()
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    }
    try { $response = $web.GetResponse() }
    catch [Net.WebException] {
        $response = $_.Exception.Response
        if (-not $response) { throw 'Hosting unavailable; publication pending.' }
    }
    try {
        $status = [int]$response.StatusCode
        $reader = New-Object IO.StreamReader($response.GetResponseStream(), [Text.Encoding]::UTF8)
        $text = $reader.ReadToEnd()
        $reader.Dispose()
    } finally { $response.Close() }
    if ($status -in @(401, 403)) { throw "Hosting rejected access ($status); publication pending." }
    $data = $null
    if ($text.Trim()) { try { $data = $text | ConvertFrom-Json } catch { $data = $null } }
    return [PSCustomObject]@{ Status = $status; Data = $data }
}

function Find-OpenPull {
    param([string]$Branch)
    for ($page = 1; ; $page++) {
        $result = Invoke-Api -Method GET -Path "/pulls?state=open&limit=50&page=$page"
        if ($result.Status -ne 200) { throw "Cannot list pull requests ($($result.Status))." }
        $items = @($result.Data)
        foreach ($item in $items) {
            if ($item -and $item.head.ref -ceq $Branch -and $item.base.ref -ceq $mainBranch) { return $item }
        }
        if ($items.Count -lt 50) { return $null }
    }
}

function Request-AutoMerge {
    param([int]$Number, [string]$Head)
    $body = @{ Do = 'merge'; merge_when_checks_succeed = $true; head_commit_id = $Head; delete_branch_after_merge = $true }
    $result = Invoke-Api -Method POST -Path "/pulls/$Number/merge" -Body $body
    if ($result.Status -eq 409 -and $result.Data -and $result.Data.message -match 'schedul') {
        # Renew an earlier schedule for the current head.
        Invoke-Api -Method DELETE -Path "/pulls/$Number/merge" | Out-Null
        $result = Invoke-Api -Method POST -Path "/pulls/$Number/merge" -Body $body
    }
    if ($result.Status -in @(200, 201, 204)) { return }
    $pull = Invoke-Api -Method GET -Path "/pulls/$Number"
    if ($pull.Data -and $pull.Data.merged) { return }
    throw "Automatic merge was not accepted for pull request #$Number ($($result.Status))."
}

function Set-PublicationState {
    param([string]$Text)
    $path = Join-Path (Invoke-Git -GitArgs @('rev-parse', '--absolute-git-dir')) 'agent-skills-publication'
    [IO.File]::WriteAllText($path, ((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') + " $Text`n"))
}

# Push local automatic commits to this clone's proposal branch and make sure
# exactly one pull request with automatic merge exists for it.
function Publish-Proposal {
    $head = [string](Invoke-Git -GitArgs @('rev-parse', 'HEAD'))
    $machine = Get-MachineName
    $branch = "agent-skills/sync/$machine-$(Get-CloneId)"
    if ((Invoke-GitCode -GitArgs @('-c', 'credential.interactive=false', 'push', '-q', 'origin', "HEAD:refs/heads/$branch")) -ne 0) {
        Set-PublicationState "pending: push of $branch failed; local commits preserved"
        throw 'Proposal branch push failed; local commits preserved for retry.'
    }
    try {
        $pull = Find-OpenPull -Branch $branch
        if (-not $pull) {
            # Windows PowerShell 5.1 reads scripts without BOM as ANSI: keep this file ASCII.
            $body = @{ head = $branch; base = $mainBranch; title = "Skill edits from $machine"
                body = 'Automatic publication of client skill edits; the hosting runs the technical checks.' }
            $created = Invoke-Api -Method POST -Path '/pulls' -Body $body
            if ($created.Status -in @(200, 201)) { $pull = $created.Data }
            # The request may have succeeded although the response was lost.
            else { $pull = Find-OpenPull -Branch $branch }
            if (-not $pull) { throw 'Pull request not created; retry later.' }
        }
        Request-AutoMerge -Number $pull.number -Head $head
    } catch {
        Set-PublicationState "pending: $($_.Exception.Message)"
        throw
    }
    Set-PublicationState "pending: pull request #$($pull.number) $($pull.html_url) head $head"
    Write-Output "PUBLISHED: pull request #$($pull.number) scheduled for automatic merge"
}

function Sync-Repository {
    $remotes = @(Invoke-Git -GitArgs @('remote'))
    if ($remotes -notcontains 'origin') { Write-Warning 'No origin; linking local catalog only.'; return }
    Invoke-Git -GitArgs @('symbolic-ref', '-q', 'HEAD') | Out-Null
    Invoke-Git -GitArgs @('rev-parse', '--verify', '-q', '@{upstream}') | Out-Null
    Assert-AutomaticCommits
    Migrate-LegacyLinks
    Publish-LocalChanges
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'fetch', '-q', '--prune', 'origin') | Out-Null
        Assert-AutomaticCommits
        Reconcile-Repository
        Migrate-LegacyLinks
        Publish-LocalChanges
        if (-not @(Invoke-Git -GitArgs @('rev-list', '@{upstream}..HEAD')).Count) { Set-PublicationState 'current'; return }
        try { Publish-Proposal; return }
        catch {
            Write-Warning $_.Exception.Message
            if ($attempt -eq 1) { throw 'Publication pending; local commits preserved for the next synchronization.' }
        }
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
    foreach ($catalog in Get-LinkRoots) {
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

function Install-Registry {
    $registry = Get-Registry
    $failed = $false
    foreach ($name in Get-LibraryNames) {
        $registered = [bool]$registry.skills.PSObject.Properties[$name]
        if (-not $registered -and -not (Test-Path -LiteralPath (Join-Path $repo "skills\$name\SKILL.md") -PathType Leaf)) { continue }
        try { Set-SkillLinks -Name $name }
        catch { Write-Warning $_.Exception.Message; $failed = $true }
    }
    if ($failed) { throw 'Some catalog skills could not be linked.' }
}

# --- Status and external update proposals ------------------------------------

function Get-Trailer {
    param([string]$Message, [string]$Key)
    if ($Message -match "(?m)^Agent-Skills-${Key}: (\S+)") { return $Matches[1] }
    return ''
}

function Get-ProposalRefs {
    param([string]$Name = '')
    $prefix = "refs/remotes/origin/$proposalPrefix/"
    if ($Name) { $prefix += "$Name/" }
    return @(Invoke-Git -GitArgs @('for-each-ref', '--sort=-committerdate', '--format=%(refname)', $prefix) | Where-Object { $_ })
}

function Show-Updates {
    $refs = @(Get-ProposalRefs)
    if (-not $refs.Count) { Write-Output 'No pending external updates.'; return }
    foreach ($ref in $refs) {
        $message = (Invoke-Git -GitArgs @('log', '--first-parent', '--grep=^Agent-Skills-Proposal: ', '-1', '--format=%B', $ref)) -join "`n"
        $incoming = Get-Trailer $message 'Incoming-Revision'
        Write-Output ("UPDATE $(Get-Trailer $message 'Proposal'): $(Get-Trailer $message 'Status'), policy $(Get-Trailer $message 'Policy'), " +
            "incoming $($incoming.Substring(0, [Math]::Min(12, $incoming.Length))), commit $(Invoke-Git -GitArgs @('rev-parse', $ref))")
    }
}

function Show-Status {
    $machine = Get-MachineName
    $assigned = 'unknown'
    try { $assigned = ((Invoke-Git -GitArgs @('show', "refs/remotes/origin/${mainBranch}:agent-skills.json")) -join "`n" | ConvertFrom-Json).server.machine } catch { }
    Write-Output "Machine: $machine; assigned server: $assigned"
    $pending = 'unknown'
    try { $pending = @(Invoke-Git -GitArgs @('rev-list', '@{upstream}..HEAD')).Count } catch { }
    Write-Output "Unpublished automatic commits: $pending"
    $state = Join-Path (Invoke-Git -GitArgs @('rev-parse', '--absolute-git-dir')) 'agent-skills-publication'
    if (Test-Path -LiteralPath $state) { Write-Output ('Publication: ' + [IO.File]::ReadAllText($state).Trim()) }
    Show-Updates
}

function Get-ReviewPath {
    param([string]$Name)
    return Join-Path (Invoke-Git -GitArgs @('rev-parse', '--absolute-git-dir')) "agent-skills-review\$Name"
}

function Show-Review {
    param([string]$Name)
    Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'fetch', '-q', '--prune', 'origin') | Out-Null
    $refs = @(Get-ProposalRefs -Name $Name)
    if (-not $refs.Count) { throw "No pending update proposal for $Name." }
    $head = [string](Invoke-Git -GitArgs @('rev-parse', $refs[0]))
    $branch = $refs[0].Substring('refs/remotes/origin/'.Length)
    $message = (Invoke-Git -GitArgs @('log', '--first-parent', '--grep=^Agent-Skills-Proposal: ', '-1', '--format=%B', $head)) -join "`n"
    $status = Get-Trailer $message 'Status'
    Write-Output "Proposal: $Name ($status), policy $(Get-Trailer $message 'Policy')"
    Write-Output "Incoming revision: $(Get-Trailer $message 'Incoming-Revision')"
    Write-Output "Proposal commit: $head (branch $branch)"
    Write-Output "  base: refs/remotes/origin/${mainBranch}:upstream/$Name  ours: refs/remotes/origin/${mainBranch}:skills/$Name  incoming: ${head}:upstream/$Name"
    Write-Output '--- Result compared with the current shared version ---'
    Invoke-Git -GitArgs @('--no-pager', 'diff', '--stat', "refs/remotes/origin/$mainBranch", $head, '--', "skills/$Name", 'sources.json')
    Invoke-Git -GitArgs @('--no-pager', 'diff', "refs/remotes/origin/$mainBranch", $head, '--', "skills/$Name")
    $worktree = Get-ReviewPath -Name $Name
    if (Test-Path -LiteralPath $worktree) {
        $current = [string](& git -C $worktree rev-parse HEAD 2>$null)
        if ($current -ne $head) {
            if (@(& git -C $worktree status --porcelain 2>$null).Count) { throw "Review copy with edits for an older proposal preserved: $worktree" }
            Invoke-Git -GitArgs @('worktree', 'remove', '--force', $worktree) | Out-Null
        }
    }
    if (-not (Test-Path -LiteralPath $worktree)) {
        Invoke-Git -GitArgs @('worktree', 'add', '-q', '--detach', $worktree, $head) | Out-Null
        $materials = "refs/remotes/origin/agent-skills/materials/$Name/$(Get-Trailer $message 'Key')"
        if ($status -eq 'conflict' -and (Invoke-GitCode -GitArgs @('rev-parse', '-q', '--verify', $materials)) -eq 0) {
            Invoke-Git -Directory $worktree -GitArgs @('checkout', '-q', $materials, '--', "skills/$Name") | Out-Null
            Invoke-Git -Directory $worktree -GitArgs @('reset', '-q') | Out-Null
        }
    }
    Write-Output "Review copy outside the installation: $(Join-Path $worktree "skills\$Name")"
    if ($status -eq 'confirm') { Write-Output "Accept exactly this result: install.ps1 -Mode Approve -Skill $Name -Commit $head" }
    else { Write-Output "Resolve: install.ps1 -Mode Resolve -Skill $Name -Commit $head -Strategy keep-ours|take-upstream|merged" }
}

function Get-Proposal {
    param([string]$Name, [string]$Commit)
    Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'fetch', '-q', '--prune', 'origin') | Out-Null
    $refs = @(Get-ProposalRefs -Name $Name)
    if (-not $refs.Count -or [string](Invoke-Git -GitArgs @('rev-parse', $refs[0])) -ne $Commit) {
        throw 'Proposal changed since review; review it again.'
    }
    $message = (Invoke-Git -GitArgs @('log', '--first-parent', '--grep=^Agent-Skills-Proposal: ', '-1', '--format=%B', $Commit)) -join "`n"
    $shared = [string](& git -C $repo rev-parse -q --verify "refs/remotes/origin/${mainBranch}:skills/$Name" 2>$null)
    if (-not $shared) { $shared = 'none' }
    if ($shared -ne (Get-Trailer $message 'Shared-Tree')) { throw 'Shared skill changed since the proposal; the server will prepare a new one.' }
    $registry = (Invoke-Git -GitArgs @('show', "refs/remotes/origin/${mainBranch}:sources.json")) -join "`n" | ConvertFrom-Json
    $policy = 'confirm'
    if ($registry.skills.$Name.auto_update) { $policy = 'auto' }
    if ($policy -ne (Get-Trailer $message 'Policy')) { throw 'Update policy changed since the proposal; the server will prepare a new one.' }
    return [PSCustomObject]@{ Branch = $refs[0].Substring('refs/remotes/origin/'.Length); Message = $message }
}

# Merge the reviewed proposal only if its inputs and result are unchanged.
function Approve-Proposal {
    param([string]$Name, [string]$Branch, [string]$Head, [string]$Result, [string]$Incoming)
    $pull = Find-OpenPull -Branch $Branch
    if (-not $pull) { throw 'The pull request of this proposal is not open.' }
    if ($pull.head.sha -ne $Head) { throw 'Proposal changed since review; review it again.' }
    if ((Invoke-GitCode -GitArgs @('merge-base', '--is-ancestor', "refs/remotes/origin/$mainBranch", $Head)) -ne 0) {
        $updated = Invoke-Api -Method POST -Path "/pulls/$($pull.number)/update?style=merge"
        if ($updated.Status -notin @(200, 201, 204)) { throw "Cannot update the proposal ($($updated.Status)); wait for the server." }
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            $pull = Find-OpenPull -Branch $Branch
            if ($pull.head.sha -ne $Head) { break }
            Start-Sleep -Seconds 1
        }
        Invoke-Git -GitArgs @('-c', 'credential.interactive=false', 'fetch', '-q', 'origin', $Branch) | Out-Null
        $Head = $pull.head.sha
        if ([string](& git -C $repo rev-parse -q --verify "${Head}:skills/$Name") -ne $Result -or
            [string](& git -C $repo rev-parse -q --verify "${Head}:upstream/$Name") -ne $Incoming) {
            throw 'Updated proposal differs from the reviewed result; review it again.'
        }
    }
    Request-AutoMerge -Number $pull.number -Head $Head
    Write-Output "ACCEPTED: $Name pull request #$($pull.number) at $Head"
}

function Approve-Update {
    param([string]$Name, [string]$Commit)
    $proposal = Get-Proposal -Name $Name -Commit $Commit
    if ((Get-Trailer $proposal.Message 'Status') -ne 'confirm') { throw 'Conflicts need -Mode Resolve with keep-ours, take-upstream or merged.' }
    Approve-Proposal -Name $Name -Branch $proposal.Branch -Head $Commit -Result (Get-Trailer $proposal.Message 'Result-Tree') -Incoming (Get-Trailer $proposal.Message 'Incoming-Tree')
}

function Resolve-Update {
    param([string]$Name, [string]$Commit, [string]$Strategy)
    $proposal = Get-Proposal -Name $Name -Commit $Commit
    $worktree = Get-ReviewPath -Name $Name
    switch ($Strategy) {
        'keep-ours' { $target = Get-Trailer $proposal.Message 'Shared-Tree' }
        'take-upstream' { $target = Get-Trailer $proposal.Message 'Incoming-Tree' }
        'merged' {
            if (-not (Test-Path -LiteralPath $worktree) -or [string](& git -C $worktree rev-parse HEAD) -ne $Commit) {
                throw "Run -Mode Review -Skill $Name and edit $worktree first."
            }
            foreach ($file in Get-ChildItem -LiteralPath (Join-Path $worktree "skills\$Name") -Recurse -File -Force) {
                if (Select-String -LiteralPath $file.FullName -Pattern '^(<<<<<<<|>>>>>>>)( |$)' -Quiet) { throw 'Conflict markers remain in the merged result; nothing published.' }
            }
            Assert-CatalogSkill -Name $Name -Root $worktree
            Invoke-Git -Directory $worktree -GitArgs @('add', '-A', '--', "skills/$Name") | Out-Null
            $target = [string](Invoke-Git -Directory $worktree -GitArgs @('write-tree', "--prefix=skills/$Name/"))
        }
        default { throw "Unknown strategy: $Strategy" }
    }
    $head = $Commit
    if ([string](& git -C $repo rev-parse -q --verify "${Commit}:skills/$Name") -ne $target) {
        # Content-only commit on top of the server proposal; pins stay server-authored.
        $temporary = Join-Path (Invoke-Git -GitArgs @('rev-parse', '--absolute-git-dir')) ('client-resolve-' + [Guid]::NewGuid().ToString('N'))
        Invoke-Git -GitArgs @('worktree', 'add', '-q', '--detach', $temporary, $Commit) | Out-Null
        try {
            Invoke-Git -Directory $temporary -GitArgs @('rm', '-rq', '--', "skills/$Name") | Out-Null
            Invoke-Git -Directory $temporary -GitArgs @('read-tree', "--prefix=skills/$Name/", '-u', $target) | Out-Null
            Invoke-ServiceGit -Directory $temporary -GitArgs @('commit', '-q', '-m', "Resolve $Name update: $Strategy`n`nAgent-Skills-Client: 1") | Out-Null
            $head = [string](Invoke-Git -Directory $temporary -GitArgs @('rev-parse', 'HEAD'))
        } finally { Invoke-Git -GitArgs @('worktree', 'remove', '--force', $temporary) | Out-Null }
        if ((Invoke-GitCode -GitArgs @('-c', 'credential.interactive=false', 'push', '-q', 'origin', "${head}:refs/heads/$($proposal.Branch)")) -ne 0) {
            throw 'Proposal push failed; resolution not published.'
        }
    }
    if ($Strategy -eq 'merged' -and $head -ne $Commit) {
        Invoke-Git -Directory $worktree -GitArgs @('update-ref', 'HEAD', $head, $Commit) | Out-Null
    }
    Approve-Proposal -Name $Name -Branch $proposal.Branch -Head $head -Result $target -Incoming (Get-Trailer $proposal.Message 'Incoming-Tree')
    if ($Strategy -eq 'merged') { Invoke-Git -GitArgs @('worktree', 'remove', '--force', $worktree) | Out-Null }
}

# --- Stopped synchronization: shown and resolved by the "Projects" UI ---------

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

# Repeats the merge of Reconcile-Repository in a disposable worktree and passes its unmerged paths to $Then.
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
# library are the object ids that were shown. The merge is published like any client edit, through
# the proposal pull request. Exit 5: the conflict changed and nothing was merged.
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
            # A hand-edited result must still be a valid skill; nothing is merged otherwise.
            if ($path -cmatch '^skills/([^/]+)/SKILL\.md$' -and (Test-Path -LiteralPath $target)) { Assert-SkillText -Name $Matches[1] -Path $target }
            Invoke-Git -Directory $worktree -GitArgs @('add', '-A', '--', ":(literal)$path") | Out-Null
        }
        if (@(& git -C $worktree diff --name-only --diff-filter=U).Count) { throw 'Unresolved files remain; nothing merged.' }
        Invoke-ServiceGit -Directory $worktree -GitArgs @('-c', "core.hooksPath=$hooks", 'commit', '-q', '--no-edit', '-m',
            "Resolve shared skill edits`n`nAgent-Skills-Client: 1") | Out-Null
        return [string](Invoke-Git -Directory $worktree -GitArgs @('rev-parse', 'HEAD'))
    }
    if (-not $merged) { Write-Warning 'The conflict changed; review it again.'; return 5 }
    Invoke-Git -GitArgs @('merge', '-q', '--ff-only', '--no-overwrite-ignore', $merged) | Out-Null
    $published = $true
    try { Publish-Proposal | Out-Null }
    catch { $published = $false; Write-Warning "$($_.Exception.Message) The next synchronization retries it." }
    if (-not (Sync-Bindings)) { Write-Warning 'Some skill links need attention; see status.' }
    Write-Json ([ordered]@{resolved = $true; published = $published})
    return 0
}
