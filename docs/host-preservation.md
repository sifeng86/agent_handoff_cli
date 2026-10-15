# Host preservation inspection

The runtime includes read-only inspection of selected Linux configuration
entries. It records private metadata and content digests before installation,
then compares the same scope afterwards. Public results contain only category
names, status and an opaque handle; they do not contain file contents, paths or
credential-derived hashes.

```bash
agentctl host inspect --checkout /path/to/project \
  --private-root /absolute/private-state
agentctl host verify --checkout /path/to/project \
  --private-root /absolute/private-state --preservation-handle HANDLE
```

No Git authority or runtime session is required. Records live outside the
checkout in private directories/files. Inspection covers explicitly listed
SSH, Git, provider, shell, networking, VPN, firewall, users/security, package
source and service configuration paths. It uses descriptor-relative reads,
does not follow symlink targets, bounds reads/tree depth, and detects changes
during observation. Unreadable paths and symlink targets are reported as
unverifiable, never as preserved. Absent selected entries remain observable.

`scripts/package_smoke.py` performs this inspection before its builds and pip
installations. It installs into fresh isolated venv prefixes, refuses existing
prefixes including symlinks, and verifies accessible configuration afterwards.
A detected change stops the smoke and retains evidence; it does not attempt
rollback or rewrite host settings. Results are included in the artifact report.

Evidence scope is deliberately explicit: selected configuration entries are
not live firewall rules, running service state, Windows VPN/network policy,
credential usability or all host configuration. Inaccessible groups prevent a
complete host-preservation claim even when isolated package installation passes.

A57 fixtures exercise existing SSH/VPN/firewall/provider/Git/network/shell
configuration across actual isolated venv creation. A58 fixtures exercise
refusal of existing-prefix replacement. Necessary destructive host repairs
with verified backup/recovery handles and minimum-change execution are still
unimplemented/unverified. Inspection handles contain observations, not backups.

Observed real WSL package installation on 2026-10-09: SSH, Git, provider, shell, network, VPN, firewall and package-source selected entries matched before/after; no observed group changed. Services and users/security had unverifiable entries. The host report therefore remained `not_ready`, while the two isolated package installation checks passed. This is partial host evidence.

## User-owned file recovery core

`host_recovery.HostFileRecovery` now provides private verified byte backups and conditional one-file restoration for Linux user-owned regular files. It rejects symlinks, hardlinks, special modes, extended attributes, unsupported ownership/groups and files over 16 MiB. Backup handles include private binary material and metadata; public API reports contain opaque handles/status only. A private byte-restoration rehearsal is required before issuing a handle.

Restore requires original and independently backed-up expected-current handles for the same path. It journals before replacement, rechecks the editor observation, restores bytes/mode/uid/gid/mtime through an atomic file replacement and fsync, and verifies the result. Pending operations block subsequent replacements. Resolution proves restored/not_restored or retains unknown; it never replays replacement or rolls back editor work. The last observation/rename race remains a filesystem limitation.

Five isolated fixture tests cover binary bytes/mode/mtime, unrelated file preservation, simulated interruption before/after rename, editor change, corrupted backup/wrong scope and link/xattr refusal. CLI wiring, two record schemas and two real process-kill boundaries are implemented; permission/durability/full crash matrix and real explicitly required host repair remain incomplete. This core is not service/firewall/VPN/Windows state recovery, and does not establish full A58 completion. No actual host configuration was modified.

CLI file recovery (explicit replacement intent; no Git authority required):

```bash
agentctl host backup --host-file /absolute/owned/config --checkout /path/to/project --private-root /absolute/private-state
agentctl host verify-backup --backup-handle ORIGINAL --checkout /path/to/project --private-root /absolute/private-state
# Back up the independently reviewed current file after a necessary repair.
agentctl host backup --host-file /absolute/owned/config --checkout /path/to/project --private-root /absolute/private-state
agentctl host restore --backup-handle ORIGINAL --expected-handle CURRENT --checkout /path/to/project --private-root /absolute/private-state
agentctl host resolve --operation-id OPERATION --checkout /path/to/project --private-root /absolute/private-state
```

Host backup/journal records live under private-root/host, with strict host-backup/host-recovery schemas and runtime cross-record guards. CLI never emits paths, private bytes or credential-derived digests. Fixture CLI tests exercise missing handles, damaged material and stale editor preconditions; subprocess tests exit 77 before/after rename and prohibit host replacement during repeated resolution, including a third real exit boundary with journaled staging before rename. This does not authorize arbitrary real host changes; explicit necessary repair/recovery scope still applies. Journal now records the exact staging observation and derives its name from the operation ID. After a crash before rename, resolution removes only matching staging material and fsyncs its directory before terminal resolution. Edited, symlinked or unproven staging is retained and keeps the operation unknown. Full durability/permission matrix remains outstanding; the last observation/unlink race is a filesystem limitation.

Staging tests also prove editor-modified staging is never published/deleted and preexisting unproven name collisions are retained. Crashes during staging creation before identity journaling may leave unproven residue; recovery reports unknown rather than guessing cleanup authority.

Durability resolution now fsyncs the exact currently observed target file and its parent directory before terminal bookkeeping, then rechecks bytes/metadata. Injected EIO on parent fsync keeps a post-rename operation prepared/unknown; successful later resolution completes without replaying replacement. Injected EACCES during target reads and editor changes during sync also preserve pending evidence and user data. These fault tests verify fail-closed behavior; they are not physical power-loss or complete permission-policy evidence.
