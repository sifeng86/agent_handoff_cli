# Security, bootstrap, and partial-handoff audit (A55–A63)

Baseline: Design v1.2 §20.9–§20.10 and §25, with spec §15–17. The complete row-level assertion inventory is in [security-bootstrap-handoff-acceptance-audit.json](security-bootstrap-handoff-acceptance-audit.json).

| ID | Classification | Finding |
| --- | --- | --- |
| A55 | proven_local | Tracked `.env` selection fails checkpoint before mutation; ref, index, and file content remain unchanged. |
| A56 | proven_local | Mapping parser and filesystem/snapshot tests reject traversal, protected paths, aliases, and symlink escape before writes. |
| A57 | proven_local | Guard fixtures and current WSL package evidence preserve the selected Linux paths. The host report remains `not_ready` when service/user groups are unverifiable; Windows and live service state are outside scope. |
| A58 | proven_local | [Actual isolated wheel launcher repair](installer-repair-evidence.json) uses the current source script: verified healthy and pre-repair backups, missing-interpreter fault, minimum one-file replacement, exact restore and unrelated-prefix preservation. 13 focused tests cover backup-before-write and failure/unknown behavior. The new wheel now has [installed-SDK proof](a58-installed-sdk-evidence.json); there is no repair CLI subcommand. Global host configuration is outside this proof. |
| A59 | proven_hosting | Actual fresh Codex and Claude sessions on isolated local Git authorities read canonical instructions and completed inspection/resume before managed feature edits. Evidence is version- and invocation-scoped. |
| A60 | proven_local | `test_A60_incompatible_protocol_readonly_diagnostic_and_mutation_refusal` invokes actual CLI inspect and release against a future protocol. Both return unsupported/code8 with a schema-valid protocol diagnostic and empty stderr, without interpreting future fields. Remote ref, private trust, source HEAD/index/edit remain unchanged. |
| A61 | proven_local | Secret read failure does not block local Git handoff/release and returns `secrets_shared=false`. The provider failure is injected. |
| A62 | proven_local | Successful fake-adapter secret convergence remains separate from injected Git `publication_unknown`; overall status stays unknown and owner pending. |
| A63 | proven_hosting | One fresh Claude Haiku 5.5 turn on isolated local Git received real resume `not_ready`/exit 6 with the required local-only key missing, then stopped before the dependent test. The launch marker, source/index/mainline and managed project checks remained clean; Primary approved the retained prose. [Scoped evidence](claude-readiness-evidence.md). |

A58's minimum-change installer repair is now proven for one isolated installed launcher; the repair module now also has installed-SDK evidence, while broader host coverage remains outstanding. A61/A62 are direct runtime API tests with a fake secret adapter; the installed package smoke does not cover partial secret handoff through the packaged CLI. A57 is limited to selected Linux configuration paths: the WSL host-preservation record reports inaccessible categories as unverifiable rather than claiming complete host preservation. A63 evidence covers one fresh Claude Haiku 5.5 turn on isolated local Git; it does not certify every provider, host, shared backend, or all §25 gates and the full v1.2 implementation.
