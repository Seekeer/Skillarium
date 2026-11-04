# Preserving local skill changes across upstream updates

Research date: 2026-10-28. Historical investigation before shared customization publishing was implemented; see the repository README for current behavior. Scope: read-only investigation; no implementation. Read `AGENTS.md`, repository/client READMEs, client update code, and server pinning code. Recommendations below are design proposals, distinguished from observed behavior.

## Recommendation

Keep server-approved pins unchanged. On each client, separate **pristine approved snapshots**, **editable installed content**, and **installation/conflict metadata**. Reconcile the last successfully applied upstream snapshot (B), current local content (L), and next approved snapshot (N). Apply clean changes in a staged skill tree; on any conflict retain the entire active skill and save B/L/N plus a proposed result outside agent discovery directories. Use existing Git for text merging, Bash/jq on Linux and native PowerShell on Windows. No Python, Node, chezmoi, or new background service is required.

Start with conservative package-manager behavior (keep local + stage incoming), then optionally enable clean three-way text merges. Never put conflict markers into an active `SKILL.md` or executable script.

## Existing behavior and constraints

- Server pins include source revision and `content_sha256`; only approved materialization is eligible for delivery. Source content is not stored in this updater repository. See [server/skills.py](../server/skills.py#L182), [approval](../server/skills.py#L227), and [README](../README.md).
- Linux installs into `versions/<name>/<approved-hash>` and points both agent discovery locations there. Existing modified target content fails verification; switching away from a modified old target also fails, preserving it. See [client.sh:44–77](../client/scripts/client.sh#L44) and [92–138](../client/scripts/client.sh#L92).
- Windows implements corresponding checks and junction ownership checks: [client.ps1:58–112](../client/scripts/client.ps1#L58), [125–188](../client/scripts/client.ps1#L125). Neither client currently merges changes.
- Consequently, editing a discovered skill edits the purportedly content-addressed cache itself. Its hash identifies the former upstream bytes, not the edited bytes. A preserved modified cache is not a trustworthy historical base.
- Removing a registry entry currently leaves old links for manual removal; the Linux registry loop visits only present entries ([client.sh:141](../client/scripts/client.sh#L141)); this is also explicitly documented in [client/README.md](../client/README.md).
- Native clients, foreign-installation preservation, and no Python on clients are explicit [AGENTS.md](../AGENTS.md) requirements. Existing Git source fetches are shallow, approved-commit fetches; a design must not depend on complete upstream ancestry.

## Primary-source comparison

| Approach | What it actually preserves | Conflict/deletion behavior | Fit here |
| --- | --- | --- | --- |
| Git three-way merge | Changes relative to a common base; independent text edits can combine | Modify/delete is a conflict and modified content is retained; tree merge tracks absence separately from empty files | Best merging primitive and deletion model; retain B explicitly |
| chezmoi | Source state, rendered target state, actual destination state | Explicit merge command invokes an external tool; apply prompts when destination changed | Borrow state separation and manual resolution; its three states are not historical B/L/N |
| Debian dpkg conffiles | Installed configuration and last distributed-file digest | Both sides changed: user resolves; user-deleted configuration stays deleted | Strong precedent for preserving edits and local deletion |
| Arch pacman backup files | Original/current/incoming digests | Both sides differ: preserve current and write `.pacnew`; removal normally saves configuration as `.pacsave` | Good unattended default, with artifacts stored outside discoverable skills |
| Vercel skills | Lock information about upstream skills; updates reinstall changed skills | Current Git install path clears destination before copying; upstream whole-skill deletion asks interactively and is skipped non-interactively | Useful contrast; not a local-edit merge solution |

### Git: historical three-way merge and modify/delete

Official Git documentation states that non-overlapping changes from the common ancestor are incorporated, while overlapping changes require resolution. `git merge-file <current> <base> <other>` combines base-to-other changes into current; zero means clean, a positive conflict count indicates conflicts, and errors must be handled separately. Its default output modifies current, so use only staged copies or explicitly capture output. Sources: [official git-merge documentation source](https://github.com/git/git/blob/master/Documentation/git-merge.adoc#L234-L243), [git-merge-file source documentation](https://github.com/git/git/blob/master/Documentation/git-merge-file.adoc#L17-L50).

Git's merge implementation explicitly reports `CONFLICT (modify/delete)` and leaves the modified version in the tree: [merge-ort.c](https://github.com/git/git/blob/master/merge-ort.c#L4417-L4424). This is an existence conflict, not necessarily overlapping text. An upstream deletion of locally edited `references/example.md` must not silently discard it. Conversely, a locally unchanged file deleted upstream can disappear cleanly.

`merge-file` operates on three existing file contents; implement existence/add/delete decisions before invoking it. An absent file is not an empty file. Do not use automatic “ours/theirs” resolution: either can discard intentional changes. A full Git tree merge could supply rename handling, but introduces repository/index state and complexity; per-path reconciliation with explicit ambiguous-rename conflicts is a practical first version.

Verification limitation: git-scm.com documentation requests returned HTTP 403 in this environment; the equivalent official git/git documentation and implementation URLs above were fetched successfully. Git links track `master` and can move.

### chezmoi: similar vocabulary, different base

The official [merge command](https://www.chezmoi.io/reference/commands/merge/) describes destination/source/target and configurable `merge.command`/`merge.args`, defaulting to `vimdiff`; it falls back to two-way merge when target computation fails. The [merge guide](https://www.chezmoi.io/user-guide/tools/merge/) defines destination as actual disk content, source as the source repository, and target as the desired rendered source. These are not old upstream, edited old upstream, and new upstream.

[Apply documentation](https://www.chezmoi.io/reference/commands/apply/) says a file modified since chezmoi last wrote it prompts before overwrite. [Update documentation](https://www.chezmoi.io/reference/commands/update/) says update pulls and applies; its default Git command is `git pull --autostash --rebase`. That repository operation must not be mistaken for automatically merging edits made to installed destination files.

Borrow explicit destination tracking, preview/diff, and an optional user-selected merge tool. Do not introduce chezmoi as a required runtime or substitute its rendered target for the historical upstream base.

### Package managers: retain current, preserve incoming separately

[Debian Policy, appendix 5.1](https://www.debian.org/doc/debian-policy/ap-pkg-conffiles.html#automatic-handling-of-configuration-files-by-dpkg) documents conffile digest comparison: an upstream-only change replaces the file; changes by both distributor and user require user resolution. It explicitly says a user-removed conffile is not replaced. This supports recording local deletion as a meaningful choice rather than treating every missing file as damage. The mechanism applies to declared conffiles, not arbitrary package files; treating skill files similarly is our proposed policy.

The official [pacman manual, HANDLING CONFIG FILES](https://man.archlinux.org/man/pacman.8.en#HANDLING_CONFIG_FILES) documents original/current/new MD5 comparisons: original=current permits upgrade; original=new preserves local; current=new is already equivalent; all different preserves current and writes incoming as `.pacnew`. Its removal section documents `.pacsave` unless `--nosave` is used. These digest comparisons choose an action; they do not perform a text merge. Preserve the policy, retaining this repository's SHA256 rather than adopting MD5.

For skills, sidecars inside an active directory can be discovered or consumed as references. Store incoming snapshots and conflict bundles in separate client state instead of scattering `.pacnew` files through active skill trees.

### Vercel skills: source-verified behavior, not an assumed guarantee

Examined official repository commit [`699d35b21bd3c97dbc0edeae062b2698898bb2e5`](https://github.com/vercel-labs/skills/commit/699d35b21bd3c97dbc0edeae062b2698898bb2e5), obtained from the GitHub commits API during research. Conclusions are scoped to these Git-backed update/install paths and this snapshot.

- Project update computes the hash of the **fetched source clone**, compares it with the lock, and checks installed-skill presence. It does not use that comparison to verify local installed bytes: [update.ts:1021–1045](https://github.com/vercel-labs/skills/blob/699d35b21bd3c97dbc0edeae062b2698898bb2e5/src/update.ts#L1021-L1045).
- Changed project skills call `installToTargets`; global updates call `installFetched`, which calls `installToTargets`; ordinary Git skills reach `installSkillForAgent`: [global update](https://github.com/vercel-labs/skills/blob/699d35b21bd3c97dbc0edeae062b2698898bb2e5/src/update.ts#L834-L845), [add.ts installer dispatch](https://github.com/vercel-labs/skills/blob/699d35b21bd3c97dbc0edeae062b2698898bb2e5/src/add.ts#L1306-L1347), [installFetched](https://github.com/vercel-labs/skills/blob/699d35b21bd3c97dbc0edeae062b2698898bb2e5/src/add.ts#L1507-L1524).
- `cleanAndCreateDirectory` recursively removes the destination and recreates it, explicitly to remove renamed/deleted files from earlier installs. Copy mode and canonical symlink-mode installation invoke it before copying: [installer.ts:190–205](https://github.com/vercel-labs/skills/blob/699d35b21bd3c97dbc0edeae062b2698898bb2e5/src/installer.ts#L190-L205), [371–405](https://github.com/vercel-labs/skills/blob/699d35b21bd3c97dbc0edeae062b2698898bb2e5/src/installer.ts#L371-L405). Thus local content can be overwritten when upstream changes; an unchanged upstream skill can instead be skipped. A lock hash is not proof of a local-edit merge facility.
- Whole skills apparently deleted upstream are listed and offered for removal. `--yes` **or** non-TTY input skips deletion, rather than authorizing removal: [promptDeletions](https://github.com/vercel-labs/skills/blob/699d35b21bd3c97dbc0edeae062b2698898bb2e5/src/update.ts#L343-L375). This differs from deleting individual files during reinstallation.

All cited HTTP endpoints in this note were successfully fetched during research except the explicitly noted git-scm.com endpoints, which are not used as evidence. Documentation/source can evolve; Vercel links are commit-pinned.

## Proposed reconciliation policy

Compare bytes and presence over the union of paths in B, L, and N. Equality includes absence; use these rules in order.

| State | Proposed action |
| --- | --- |
| L = N | Keep the equivalent result |
| L = B | Take N, including upstream deletion |
| N = B | Keep L, including intentional local deletion/addition |
| B present; L and N present; both changed differently | Attempt supported text three-way merge; otherwise conflict |
| B present; L modified; N absent | Modify/delete conflict; preserve L |
| B present; L absent; N modified | Delete/modify conflict; preserve local absence until resolved |
| B absent; L and N present with different bytes | Add/add conflict; do not guess a common ancestor |
| File/directory replacement, unsafe path, unsupported encoding/binary change | Explicit conflict/error; preserve active installation |

A local-only file survives upstream updates. If upstream later introduces that same path with different contents, it is add/add. A probable rename can initially be treated as delete+add: a local modification to the old path prevents publication pending resolution. Do not claim rename-aware merging until implemented and tested.

## Practical architecture for existing pins and native clients

1. **Preserve the server contract.** Server selects approved N exactly as today. Clients never choose latest source branches and never upload local edits into global pins. Record catalog/source identity as well as skill name, so a name reused for another source is a migration conflict.
2. **Maintain pristine bases.** Keep verified, upstream-only content in `versions/<name>/<hash>`. Keep editable installed generations under a distinct managed location, with both agent links targeting the same generation. Track applied upstream revision/hash, local generation, ownership, and pending target in JSON outside skill payload. Hashes alone detect change but cannot reconstruct B; retain its bytes.
3. **Migrate conservatively.** When an old hash-named cache contains edits, preserve it and recover pristine B by its recorded old revision/hash into another location. Verify B before use. If the old source is unavailable, especially a changed single-file URL, report “base unavailable” and stage N without guessing or overwriting. Do not relabel edited bytes as verified upstream. Avoid replacing existing links until ownership and migration are established.
4. **Stage and publish per skill.** Under a client-wide lock, snapshot L, obtain verified N, build the complete candidate, and validate required `SKILL.md` structure/path restrictions. Before publishing, verify L still matches the captured snapshot; a timer lock alone cannot stop an editor. If content changed, abort/retry. A remaining check-to-publish race requires a documented edit workflow or stronger coordination; do not promise perfect concurrent-edit isolation.
5. **Use Git only as a merge primitive.** Both platforms already require Git. Run `git merge-file` on staged regular text files and accept only exit zero. Explicitly classify other statuses as conflicts/errors; PowerShell must capture `$LASTEXITCODE` without assuming every positive code is success. Use native byte-preserving I/O, not PowerShell 5.1 default text encodings. Start with UTF-8 text; route unsupported encodings/binaries to manual handling. Do not normalize CRLF merely to make approved hashes match, run skill scripts/hooks, or honor upstream custom merge drivers.
6. **Keep conflicts out of active content.** Store exact B/L/N snapshots, path-level reason, desired pin, and candidate under `conflicts/<catalog>/<skill>/<target>/`. Repeated hourly runs reuse/report the pending conflict without accumulating copies. Retain the current entire skill on conflict, so a newly updated script cannot be paired with old conflicted instructions. Continue processing unrelated skills.
7. **Resolve explicitly.** A future native status/diff/resolve interface should allow keep-local, take-approved, manual merged content, or accept deletion. Resolution must validate current local and pending versions before publishing. After successful merge or explicit keep-local resolution, advance B to N; remaining differences become the overlay against N. While unresolved, leave B at the last applied version. Record applied upstream hash separately from effective local hash: merged content is locally customized, not byte-identical server-approved content.
8. **Publish links recoverably.** Linux can stage a sibling symlink then rename it as existing code does. Windows junction replacement requires a journal/rollback strategy; do not assume POSIX atomic rename semantics. Two discovery roots cannot be updated as one atomic operation on either platform: record each switch and recover partial publication. Retain old generations and bases until no active link, rollback, or conflict references them.

### Entire skill deletion and retirement

Distinguish three cases:

- **Source skill disappears but registry still pins an old version:** do not infer a deletion instruction from a failed fetch or missing branch path. Retain the installed approved version and report source availability. Source history rewriting or network errors must not trigger uninstall.
- **Catalog removes an entry:** current policy is manual removal. Preserve that default for compatibility, marking the managed installation “orphaned”; a complete previous-install manifest is needed to detect this reliably. Prefer explicit server retirement metadata/tombstones if automatic retirement is later desired.
- **Explicit retirement is implemented later:** unchanged owned content may be unlinked with recoverable retention. Modified content must remain preserved with a retirement conflict until the user chooses archive-and-disable, keep as detached local skill, or deletion. Archiving only outside discovery disables it; retaining its link keeps it active. Make that distinction visible. Never remove a foreign directory/link, follow a junction to recursively delete its target, or garbage-collect unresolved local content.

A retired skill's missing `SKILL.md` cannot be treated as an ordinary clean file deletion while leaving a supposedly valid active skill. Handle whole-skill retirement before per-file publication. Local removal of an entire managed skill should likewise be an explicit disabled state/tombstone if it is meant to survive hourly sync, rather than an unexplained missing path.

## Acceptance scenarios for a future implementation

Use temporary catalogs, bare remotes, and isolated cache/home directories as required by AGENTS.md. Cover clean upgrade; local-only edit; disjoint/overlapping text changes; modify/delete and delete/modify; same/different add/add; local-only additions; local deletion; unchanged and modified retirement; missing historical B; reused skill name/source change; binary/encoding/path collisions; concurrent editor changes; repeat conflicts; interrupted publication and rollback; two discovery roots; foreign ownership. Test byte-identical SHA256 behavior on both clients, including CRLF and Unicode paths. Real Windows is necessary for junction replacement/recovery; PowerShell on Linux is insufficient evidence for those filesystem semantics.

The smallest useful delivery is state separation plus preserved incoming snapshots and conflict reporting. Clean text merging can follow without altering server pin selection or adding client runtimes.
