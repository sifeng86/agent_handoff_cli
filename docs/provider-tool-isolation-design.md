# Provider tool isolation

A35/A54 remain open. Prior capability disclosures and auto-review rejections remain recorded. One explicitly authorized exclusive-MCP Haiku turn subsequently ran and failed the workflow gate; see [retained failure evidence](claude-mcp-auth-failed-evidence.json).

## Enforced filesystem component

`scripts/landlock_tool_child.py` applies Linux Landlock in a tool child, then executes a command with a fixed environment. It grants file reads individually, directory enumeration separately, and a fixed system runtime exception. Protected paths and the private policy directory cannot overlap grants. Policy errors or unavailable enforcement stop execution.

The current WSL kernel reports ABI 1. Nine focused tests execute actual restricted children and cover approved reads, blocked context/symlink/unapproved `.env` reads, descendant inheritance, environment replacement, `/proc` access, inherited descriptors, and ordinary write/link/rename refusal. An inherited FD above a subsequently lowered `RLIMIT_NOFILE` is closed using `close_range`; modeled setup/platform failures never execute the target.

ABI 1 does **not** enforce complete file integrity, truncation, metadata mutation, networking, or signal isolation. This component cannot safely expose arbitrary shell or Python execution. The [kernel documentation](https://docs.kernel.org/userspace-api/landlock.html) explains inheritance, preopened descriptor limitations and the later ABI requirement for truncation protection.

## Required provider integration

The trusted provider process keeps its account authentication outside tool children. Disable native filesystem/command tools with `--tools ""`; use `--strict-mcp-config` and one explicit fixture MCP configuration. These flags are described in the [Claude CLI reference](https://code.claude.com/docs/en/cli-reference). Installed Claude Code 2.1.296 advertises these flags in its local help; no model call was needed to check them.

The proposed stdio broker exposes two bounded tools:

- `read_file`: invoke a fixed trusted reader under the Landlock policy. The model supplies a filename, never an executable, environment, policy, or grant. Protected-file attempts must fail at the kernel boundary. Public grants exclude secret material and are rebuilt from trusted fixture state after resume.
- `agentctl`: dispatch argv through the existing A35 scoped engine proxy. The proxy retains the private context path and performs approved inspect/status/resume/secrets diagnostics outside the reader domain. Validate structured public results before returning them; return fixed errors instead of raw stderr or tracebacks.

Do not expose shell commands, arbitrary code, executable selection, filesystem writes, resource templates, account configuration, or unrestricted Doppler operations. The [Claude MCP documentation](https://code.claude.com/docs/en/mcp) describes the local stdio transport; the implemented broker now has offline protocol and dispatch tests.

The trusted spawner must clear credential, loader, Python and proxy variables **before** Python bootstrap, use `-I -S -B`, provide `stdin=DEVNULL` for readers, capture bounded stdout/stderr, close descriptors, and enforce a process deadline. Replacing the target environment after bootstrap does not establish these guarantees by itself. Broker stdin/stdout carry MCP messages; reader subprocesses must not inherit that channel.

## Gates before an API test

1. Local MCP initialization, tool discovery and dispatch work without a provider/model.
2. Private context read attempts through every exposed tool fail without disclosing its synthetic marker; caller-supplied grants, shell execution and unsafe argv are rejected.
3. Actual engine readonly discovery/resume and missing-auth diagnostics still work while private files remain unreadable through reader tools.
4. Broker outputs pass public validation and contain no capability, account credentials or raw private diagnostics. Timeout/error paths are bounded and fail closed.
5. The actual provider launch configuration exposes only the approved broker tools; trace parsing/verifiers understand their native events. Preserve source/main/index and existing provider settings.

Only after these gates pass may a fresh provider test be considered through normal approval review. Resuming development did not authorize capability disclosure or bypass the rejected action. Neither this design nor the nine component tests closes A35/A54 or proves complete host/provider isolation.

## Executed offline integration

`provider_tool_broker.py` and its Landlock child pass 22 focused tests after Primary review. Initialization bypasses, cwd mismatches, wrong-command replies, stdout/stderr overflow, pipe errors and timeouts are rejected with bounded cleanup. `resume` is an approved ownership operation, not a readonly command.

[Actual engine evidence](provider-broker-engine-evidence.json) uses an installed `agentctl`, a real isolated local Git authority, and a synthetic Doppler missing-auth CLI. A trusted stdio client reads canonical instructions, is denied context access, executes readonly inspect/status, resumes with proven ownership, and obtains `authentication_required` from secret verification. Private context is absent from captured MCP output; source/index/main and the exact owner are checked. The initial harness failure used one lock for nested proxies; separate configs fix that fixture deadlock. The context-reader call signature was also corrected before the final passed run. No model or real Doppler API call occurred.

The v2 broker now refreshes only the immutable approved public-file set after an owned resume. The refresh binds exact managed-checkout paths through a private sidecar and fails closed if policy and sidecar disagree on restart. Its two-file atomic update is deliberately not transactional: interruption can leave a mismatch that requires operator diagnosis. The installed-engine evidence above verifies broker restart, managed canonical-file reads, and denial of context, `.env`, and unapproved files.

## MCP-only profile preflight

`provider_mcp_profile.py` writes a private single-server configuration and builds an exact Haiku 5.5 CLI command with native tools disabled. The MCP server starts through `/usr/bin/env -i` before Python bootstrap. An actual local launch of this generated configuration completed stdio initialization and listed exactly `read_file` and `agentctl`; inspection of the live child environment confirmed the synthetic parent secret and PYTHONPATH were absent. See [profile preflight](provider-mcp-profile-preflight.json). No Claude model turn ran.

Focused profile tests cover exclusive configuration, model/registry checks, event correlation and public exit codes. Failed tool text is preserved for privacy scanning; nontext results are rejected. Trace path normalization is lexical and never grants or reads a file; live access remains broker/kernel policy. The controller scans whole raw and decoded output before normalization, including failures, bounds stdin/stdout/stderr, and cleans up descendants even after the leader exits. New turns also retain private pre-turn Git facts and attempt preservation checks on failure; unavailable evidence is not reported as preservation.

The authorized turn's native registry contained exactly the two approved tools and the exact model. Context was absent from raw and decoded stdout/stderr. All three read requests received `invalid request`; no engine call, owned resume or authentication diagnostic occurred. Claude's credentials-file snapshot changed while Doppler host configuration remained unchanged. The turn predates the new failure-preservation checks, so complete source/index/main/owner pre/post proof is unavailable. No second model turn has run.

Offline compatibility fixes accept inert optional request `_meta`, observed `thinking_tokens`/`rate_limit_event` telemetry and the server's `source: dynamic` field. The [MCP schema](https://modelcontextprotocol.io/specification/2025-11-25/schema) allows request metadata; the actual wire request was not retained, so metadata remains a suspected cause of the rejection. Initialization must still be first, telemetry cannot hide tool messages, failed relative reads remain failed, and extra tools/servers are rejected. Replaying the retained trace through the corrected parser still fails `single_owned_resume_unproven`. The actual engine smoke subsequently passed with metadata-bearing requests; this does not establish that a fresh model workflow now succeeds.

A second independently authorized fresh turn completed a separate canonical read, owned resume and synthetic `authentication_required` diagnostic. Raw/decoded context privacy and all source/index/main/owner/provider/Doppler-host guards passed. Primary reviewed its prose: it asks for external authentication without asking the user to operate agentctl/Git recovery. The full workflow still fails because no explicit readonly inspect/status call preceded resume; see [second turn evidence](claude-mcp-auth-second-evidence.json). Canonical instructions now name that required command and separate call explicitly; 89 focused tests and a fresh offline actual-engine fixture passed. No post-clarification model turn has run.
