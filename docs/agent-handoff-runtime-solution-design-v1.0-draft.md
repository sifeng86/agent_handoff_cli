# Agent Handoff Runtime
## Solution Design v1.0 — Draft for Iterative Brainstorming

**Status:** Draft / Scope-Frozen Candidate  
**Document type:** Solution Design  
**Version:** 1.0-draft  
**Supersedes:** `multi-agent-shared-development-runtime-solution-design-v0.1.md`

## 1. Product Definition

Agent Handoff Runtime is a lightweight coordination layer for sequential AI-assisted software development across multiple machines and agent providers.

Typical environment:

- Computer A — Codex account A
- Computer B — Codex account B
- Computer C — Claude Code

Only one agent actively modifies the project at a time.

The target user experience is:

```text
Computer A

User:
今天先做到這裡。

Agent A:
已保存進度。
```

Later:

```text
Computer B

User:
繼續開發。

Agent B:
自動恢復上一個 Agent 的開發狀態，
取得寫入權，
讀取目前 Plan 與進度，
然後繼續。
```

The human should not need to know:

- `agentctl` commands,
- Git coordination details,
- lock implementation,
- checkpoint mechanics,
- state-file format,
- recovery commands.

Core principle:

> **`agentctl` is operated by agents, not by humans.**

---

## 2. Scope Guard

The system exists for one purpose:

> **Manage the handoff of development work between AI coding agents.**

It is not intended to become:

- an AI project manager,
- an architecture decision engine,
- an autonomous product manager,
- a bug diagnosis framework,
- a CI/CD replacement,
- a Git hosting platform,
- or a general-purpose agent orchestration framework.

A feature belongs in `agentctl` only if it is needed for safe continuation across agents, sessions, accounts, or computers.

### Handoff Boundary Test

Ask:

> **If the same agent on the same computer simply continued working, would this problem still exist?**

If **yes**, it is normally a project-development concern.

Examples:

```text
Test file has a bug
→ project concern

Should the product support Google Home?
→ product concern

Redis vs PostgreSQL?
→ architecture concern
```

If **no**, it is probably a handoff/control-plane concern.

Examples:

```text
Which machine owns write access?
→ handoff concern

Which checkpoint should the next agent resume?
→ handoff concern

Previous agent stopped without releasing ownership.
→ handoff concern

Two machines contain conflicting handoff states.
→ handoff concern
```

This boundary is a core v1 architectural constraint.

---

## 3. Core Product Promise

A new agent should be able to open the repository and, after the user says:

> 繼續開發

automatically:

1. recognize that the repository uses Agent Handoff Runtime,
2. invoke the required `agentctl` workflow,
3. synchronize the repository,
4. establish safe write ownership,
5. discover the active Plan,
6. discover the current Step,
7. read the previous handoff information,
8. inspect the latest shared checkpoint,
9. load only the necessary project context,
10. continue development.

The outgoing and incoming agents do not need to share native chat history.

---

## 4. Fundamental Principles

### 4.1 Conversation is temporary; repository state is durable

Native AI sessions are not the source of truth.

### 4.2 Human uses natural language; Agent uses `agentctl`

```text
Human
  ↓
Natural language
  ↓
Coding Agent
  ↓
Handoff protocol
  ↓
agentctl
  ↓
Git / repository
```

### 4.3 Single writer

At most one active agent may own write access to the project.

Parallel code-writing agents are out of scope for v1.

### 4.4 Handoff is a control-plane concern

`agentctl` coordinates:

- ownership,
- continuation,
- checkpoints,
- handoff state,
- cross-machine recovery.

It must not interpret project semantics.

### 4.5 Project content is opaque to `agentctl`

To `agentctl`, values such as:

```text
P-012
S3
"Auth integration test still failing"
```

are metadata.

It does not need to understand authentication, HTTP, databases, UI behavior, or business requirements.

### 4.6 Keep v1 small

If removing a feature does not break the fundamental handoff experience, the feature should probably not exist in v1.

---

## 5. Zero-Operation Human UX

Normal development should require zero manual `agentctl` operations by the user.

The user interacts with the coding agent using natural language:

```text
繼續開發
```

```text
今天先做到這裡
```

```text
開始做之前規劃的 notification feature
```

The user should never be expected to type:

```text
agentctl resume
agentctl checkpoint
agentctl handoff
agentctl unlock
git rebase
git push --force
```

Those are agent/control-plane operations.

---

## 6. Decision-Only Human Intervention

When a **handoff-specific** ambiguity cannot be safely resolved, the agent must:

1. inspect the available technical state,
2. preserve recoverable copies before asking whenever possible,
3. reduce the ambiguity to a small set of understandable options,
4. explain the impact of each option,
5. ask the human to choose,
6. execute the chosen option automatically.

Example:

```text
Agent:

上一個 Agent 留下了一份未完成修改，
但 remote 上有一份較新的安全 checkpoint。

我已經先保存兩份狀態。

A. 保留未完成修改並嘗試整合
   - 不直接丟失上一個 Agent 的工作
   - 可能需要額外修復

B. 從最後已驗證 checkpoint 繼續
   - 最安全
   - 會放棄上一個未完成 step 的部分工作

User:
B
```

The agent performs all required recovery operations automatically.

### Strict Boundary

Decision-Only Human Intervention applies only to handoff/control-plane ambiguity.

It does **not** apply to:

- a buggy test,
- a buggy source file,
- whether to add Google Home support,
- architecture selection,
- dependency selection,
- API design,
- product scope,
- database selection,
- UI behavior.

Those remain normal Coding Agent ↔ Human conversations.

---

## 7. Major Components

v1 has only four conceptual components:

1. repository bootstrap instructions,
2. `agentctl`,
3. Plan files,
4. minimal handoff state.

Git provides persistence and synchronization.

---

## 8. Repository Bootstrap

A repository must contain enough instructions for a newly opened coding agent to discover the handoff protocol.

This cannot depend exclusively on an externally installed Agent Skill.

Reason:

- a new computer may not have the Skill installed,
- Codex and Claude may use different Skill mechanisms,
- provider tooling may change.

Therefore the repository itself carries the minimum bootstrap contract.

### `AGENTS.md`

The canonical provider-neutral bootstrap should conceptually say:

```text
This repository uses Agent Handoff Runtime.

Before modifying project files in a new development session:
- restore current handoff state,
- obtain write ownership,
- resume the active Plan and Step.

Do not ask the user to restate previous development progress.

Manage checkpoints and normal handoff automatically.

Do not delegate agentctl or Git recovery operations to the user.
```

### `CLAUDE.md`

`CLAUDE.md` should be a thin adapter that points Claude to the same canonical protocol.

It must not contain a second independent coordination design.

### Agent Skill

An Agent Skill may improve reliability, but:

> **The Skill is an optimization, not a hard runtime dependency.**

The repository bootstrap remains mandatory.

---

## 9. `agentctl`

`agentctl` is an agent-facing control-plane CLI.

It is not the normal human interface.

Its responsibilities are intentionally limited to:

- initialization,
- state discovery,
- ownership,
- resume,
- checkpoint,
- handoff,
- safe handoff recovery,
- active Plan pointer,
- current Step pointer,
- minimal status reporting.

### `agentctl` Must Not Do in v1

It must not:

- decide architecture,
- choose product features,
- diagnose project bugs,
- decide whether a test is correct,
- rewrite project requirements,
- select databases,
- choose dependencies,
- evaluate business priority,
- automatically select the next feature,
- replace CI,
- replace an issue tracker.

---

## 10. Suggested v1 Repository Layout

```text
project/
│
├── AGENTS.md
├── CLAUDE.md
│
├── .agent/
│   ├── config.yaml
│   ├── state.json
│   └── protocol.json
│
├── plans/
│   ├── P-001-login.md
│   ├── P-002-dashboard.md
│   └── P-003-notifications.md
│
└── project source files...
```

No Plan Registry is required in v1.

No execution journal is required.

No model-generated memory database is required.

---

## 11. Plan Model

### 11.1 No Plan Registry in v1

The filesystem is sufficient initially.

```text
plans/P-001-login.md
plans/P-002-dashboard.md
plans/P-003-notifications.md
```

If future real-world usage proves that hundreds of Plans require an index, one can be added later.

### 11.2 Immutable Plan IDs

Plans use:

```text
P-001
P-002
P-003
...
```

IDs are not reused.

Single-writer ownership means v1 does not need a complex distributed Plan-ID allocator.

### 11.3 Plan Document

A Plan answers:

> **What are we intending to build?**

Suggested structure:

```text
# P-012 Notification Feature

Status: READY

## Goal
...

## Scope
...

## Non-goals
...

## Steps

### S1
...

### S2
...

## Acceptance Criteria
...
```

### 11.4 Durable Plan Status

To avoid duplicated truth, v1 uses only:

```text
DRAFT
READY
DONE
```

There is deliberately no durable `ACTIVE` status in the Plan document.

Why?

Because active execution is runtime state.

This prevents:

```text
Plan file says ACTIVE
runtime says another Plan is active
```

### 11.5 Active Plan

The current execution state contains:

```text
active_plan = P-012
```

A Plan may remain `READY` while it is the active Plan.

When complete:

```text
Plan status → DONE
active_plan → null
```

This gives one canonical source for current execution.

---

## 12. Plan Activation

The human never needs to run an activation command.

Example:

```text
User:
開始做之前規劃的 notification feature。
```

The Coding Agent:

1. identifies the matching READY Plan,
2. establishes ownership,
3. activates it through the runtime,
4. reads the Plan,
5. begins implementation.

If multiple Plans match the user's wording, the agent may ask a normal project-intent question.

That is not a handoff decision.

### No Autonomous Next-Plan Selection

When a Plan finishes:

```text
active_plan = null
```

The agent must not arbitrarily start another READY Plan.

The next Plan requires human intent.

---

## 13. Meaning of “Continue Development”

When the user says:

> 繼續開發

the agent follows:

### Case A — Active Plan exists

Resume it automatically.

### Case B — Unfinished handoff state exists

Recover and resume it.

### Case C — No active Plan and no unfinished work

Do not silently activate another READY Plan.

The agent may briefly state that there is no active implementation and mention READY Plans if useful.

This is normal project conversation, not handoff ambiguity.

---

## 14. Step Model

Plans may contain:

```text
S1
S2
S3
...
```

The runtime stores only the current execution cursor:

```json
{
  "active_plan": "P-012",
  "current_step": "S3"
}
```

The Plan itself does not need live progress checkboxes.

Separation:

```text
Plan
= intended work

Runtime state
= current cursor

Git
= actual implementation
```

---

## 15. Minimal State

`.agent/state.json` is intentionally small.

Conceptually:

```json
{
  "active_plan": "P-012",
  "current_step": "S3",
  "status": "working",
  "last_checkpoint": "abc123",
  "handoff": {
    "summary": "S1 and S2 complete. Continue S3.",
    "note": "Relevant API endpoint already exists."
  }
}
```

Exact schema is deferred.

It must not become:

- a complete history,
- a journal,
- an agent transcript,
- an exhaustive event log.

Historical implementation information belongs in Git history.

### Runtime Status

Persisted state should remain small:

```text
idle
working
blocked
```

`blocked` is opaque to `agentctl`.

---

## 16. Project Bug Boundary

Bug-repair policy belongs to the Coding Agent / repository instructions, not to `agentctl`.

For example, `AGENTS.md` may say:

```text
If repeated attempts to fix the same project bug fail,
stop making speculative changes and mark the work blocked.
```

Then runtime state may simply contain:

```text
status: blocked
handoff summary:
"Auth integration tests still fail after repeated repair attempts."
```

The incoming agent handles the bug as normal project work.

`agentctl` does not decide:

- whether the test is wrong,
- whether production code is wrong,
- what debugging strategy to use.

---

## 17. Ownership Model

The runtime uses exclusive write ownership.

Any operation that modifies shared project state requires ownership.

Read-only inspection does not necessarily require ownership.

### Remote Coordination Requirement

Ownership must be visible across computers.

Therefore it cannot exist only in:

- a local file,
- local process memory,
- or an unpushed source branch.

A Git-remote-backed ownership record is the preferred v1 direction.

Exact Git plumbing is deferred to the Implementation Specification.

### Ownership Is Separate from `state.json`

`state.json` is checkpointed handoff state.

Ownership is live coordination state.

Ownership must be:

- remotely visible,
- mutually exclusive,
- releasable independently,
- recoverable after a crash.

This is the one special live-control mechanism strongly justified in v1.

---

## 18. No Heartbeat Daemon in v1

v1 intentionally does not require:

- a background daemon,
- filesystem watcher,
- continuous heartbeat,
- automatic autosave service.

Reason: cross-platform complexity is too high for the first version.

### Consequence

If Agent A crashes or loses quota without releasing ownership, Agent B may not be able to prove that Agent A has stopped.

This is an accepted v1 limitation.

Agent B may ask:

```text
上一個開發環境仍然顯示為持有寫入權。

A. 上一個 Agent 已停止，可以由這台接手
B. 上一個 Agent 仍在工作，這台先不要接手
```

The user chooses A or B.

The user never runs an unlock command.

If A is chosen, the Agent performs ownership recovery itself.



---

## 19. Checkpoint Model

A checkpoint is the primary durability mechanism.

Definition:

> A meaningful development state has been committed and pushed to the shared Git remote, together with enough handoff state for another agent to continue.

Conceptually:

```text
meaningful implementation unit
↓
update minimal handoff state
↓
commit
↓
push
↓
record safe checkpoint
```

### Checkpoint Frequency

Agents should checkpoint after a coherent implementation unit.

Examples:

```text
schema finished
→ checkpoint

service implemented
→ checkpoint

endpoint migrated
→ checkpoint
```

Not every file save needs a checkpoint.

### Granularity Rule

The Plan should be decomposed so that:

> Unexpected termination loses at most one small incomplete implementation unit.

This is the primary v1 protection against quota exhaustion.

---

## 20. No Autosave Daemon in v1

v1 does not continuously push every local modification.

This is a deliberate tradeoff.

If an agent disappears unexpectedly:

- everything up to the latest shared checkpoint is safe,
- the current uncheckpointed unit may exist only on the previous computer.

Cross-machine recovery of changes that were never pushed is not guaranteed.

### Why This Is Accepted

Reliable background autosave would require additional complexity:

- process lifecycle,
- Windows/WSL/macOS/Linux behavior,
- credential handling,
- snapshot cleanup,
- network failure handling,
- filesystem watchers,
- autosave refs.

v1 should not solve this unless actual usage proves it necessary.

---

## 21. Normal Handoff

Normal handoff occurs when the outgoing agent has time to stop cleanly.

Example:

```text
User:
今天先到這裡。
```

The Agent should automatically:

1. preserve current work,
2. create a checkpoint,
3. update the current Plan/Step cursor,
4. write a compact handoff summary,
5. push,
6. release ownership.

The human performs none of these steps.

### Incomplete Step Handoff

The current Step does not need to be complete.

If the user stops midway through S3, the outgoing agent may create a WIP checkpoint.

Example:

```text
P-012
S3 partially implemented
```

This is preferable to leaving useful work only on the local computer.

### WIP Checkpoint vs Completed Checkpoint

Both are durable.

```text
Completed checkpoint
→ coherent unit completed

WIP checkpoint
→ safe transfer snapshot; work remains incomplete
```

A checkpoint does not automatically mean:

- tests passed,
- feature complete,
- release ready.

Checkpoint means only:

> shared recoverable handoff state exists.

---

## 22. Unexpected Termination

Unexpected termination includes:

- quota exhaustion,
- agent process crash,
- network loss,
- computer shutdown,
- browser/session loss.

If no final handoff occurs:

```text
latest shared checkpoint
= safe shared recovery point
```

The next agent resumes from it unless another clearly newer safe shared state exists.

### Local Uncheckpointed Changes

If Agent A modifies files but never pushes them, Agent B on another machine cannot reconstruct those bytes.

This is a physical limitation, not something the protocol should pretend to solve.

v1 therefore explicitly guarantees:

> Recovery to the latest shared checkpoint, not recovery of arbitrary local-only work.

Mitigation:

- small Steps,
- frequent semantic checkpoints.

---

## 23. Resume

Resume is the central incoming-agent operation.

Conceptually it should:

1. detect repository protocol,
2. synchronize safe remote state,
3. inspect ownership,
4. determine whether previous ownership was released,
5. establish ownership if safe,
6. identify the active Plan,
7. identify the current Step,
8. read the compact handoff,
9. inspect the latest checkpoint,
10. return an agent-friendly context capsule.

### Agent-Friendly Output

Conceptual structured output:

```json
{
  "result": "ready",
  "active_plan": "P-012",
  "current_step": "S3",
  "status": "working",
  "last_checkpoint": "4f8a921",
  "handoff": {
    "summary": "S1/S2 complete. S3 is partially implemented."
  }
}
```

The coding agent then loads only relevant project context.

---

## 24. Lazy Context Loading

Incoming agents should not load:

- full chat histories,
- every historical Plan,
- every ADR,
- all Git history.

They should initially load:

1. current Plan,
2. current Step,
3. current handoff,
4. recent relevant Git changes,
5. relevant source files.

Additional history is loaded only when needed.

This keeps token overhead low.

---

## 25. Git Operating Model

Git is the durable store.

v1 should not become a general Git workflow engine.

`agentctl` needs only enough Git logic to support safe handoff.

### Automatically Safe Cases

Examples:

```text
local == remote
→ continue

local behind remote and fast-forward is possible
→ fast-forward

clean repository + valid remote checkpoint
→ continue
```

### Handoff-Related Divergence

If incompatible histories exist specifically because different development environments contain conflicting handoff states, the runtime may enter a Handoff Decision flow.

Before asking the human, preserve recoverable states when possible.

### Project Git Conflicts Are Different

If a coding agent intentionally performs normal project Git work and hits a conflict, that is ordinary software-development work.

Examples:

- merging upstream,
- cherry-picking a project commit,
- resolving code semantics.

Do not treat every Git conflict as an `agentctl` conflict.

Apply the same boundary test:

> Would this conflict exist without the agent handoff?

---

## 26. No Automatic Semantic Conflict Resolver in v1

v1 should not attempt to understand and solve arbitrary semantic Git conflicts.

If a handoff-specific state cannot be safely resolved mechanically:

1. preserve recoverable state,
2. reduce the issue to high-level choices,
3. ask the human only when necessary,
4. let the coding agent execute the chosen recovery path.

The user never resolves Git manually.

---

## 27. Plan Creation

A coding agent may create a new Plan during normal project work.

Example:

```text
plans/P-013-home-automation.md
```

Creating a Plan is not the same as implementing it.

A new Plan may remain:

```text
READY
```

until the user expresses intent to begin implementation.

### Cross-Agent Example

Agent A creates:

```text
P-013-notifications.md
Status: READY
```

Then stops.

Later:

```text
User:
開始做之前規劃的通知功能。
```

Agent B:

1. resumes handoff state,
2. sees no active Plan,
3. identifies P-013 from the user's intent,
4. activates it,
5. begins implementation.

No Plan Registry is needed.

---

## 28. Ambiguous Plan Selection

If several Plans could match the user's request, the coding agent may ask which one is intended.

Example:

```text
你是指：
A. notification center
B. email notifications
```

This is normal project intent clarification.

It is not Decision-Only Handoff Intervention.

The distinction matters.

---

## 29. Project Blockers

A coding agent may encounter:

- repeated failing tests,
- contradictory requirements,
- external API uncertainty,
- design questions,
- product decisions.

These are project-work concerns.

If the session ends while blocked, `agentctl` stores only opaque transfer state.

Example:

```text
status: blocked

handoff:
"Payment integration test still fails.
Previous agent stopped after repeated repair attempts."
```

The next agent interprets the project issue.

`agentctl` does not.

---

## 30. Source of Truth Rules

To avoid ambiguity:

### Project implementation truth

```text
Git source code + tests
```

### Feature intent

```text
Plan Markdown
```

### Current execution cursor

```text
.agent/state.json
```

### Live write ownership

```text
remote ownership record
```

### Historical implementation

```text
Git history
```

### Native chat session

```text
not a source of truth
```

---

## 31. Avoid Duplicate Truth

v1 must not store the same canonical fact independently in multiple places.

Bad:

```text
Plan file says ACTIVE
state says P-019
```

Bad:

```text
two files both claim to be the current Step
```

Resolved model:

```text
Plan status:
DRAFT / READY / DONE

Active Plan:
state only

Current Step:
state only

Ownership:
remote ownership record only
```

---

## 32. Compact Handoff Summary

A normal handoff summary should remain short.

Preferred structure:

```text
Done
Current
Next
Important note
```

Example:

```text
Done:
S1 and S2.

Current:
S3 callback handler partially implemented.

Next:
Finish callback error handling and run auth tests.

Important:
Existing route is /api/auth/callback.
```

No transcript is needed.

Handoff state is current transfer context, not permanent project documentation.

---

## 33. Token-Cost Model

v1 should minimize coordination token usage.

### Deterministic / zero-model work

Where possible:

- ownership,
- Plan pointer,
- Step pointer,
- Git SHA,
- checkpoint metadata,
- synchronization.

### Model work

The coding agent handles:

- coding,
- planning,
- debugging,
- a short handoff summary.

No dedicated cheap-model summarizer is required in v1.

That can be added only if real usage justifies it.

---

## 34. Explicitly Rejected v1 Complexity

v1 does **not** include:

- `JOURNAL.md`,
- native chat synchronization,
- memory database,
- background autosave,
- heartbeat daemon,
- full Plan Registry,
- Epic hierarchy,
- automatic next-Plan selection,
- semantic bug fingerprinting,
- deterministic repair-attempt engine,
- model router,
- cheap summarizer service,
- semantic Git conflict engine,
- CI orchestration,
- issue tracker integration,
- PR automation.

---

## 35. Initial Environment Setup

Normal daily use must be zero-operation for the human.

Initial environment provisioning is separate from normal usage.

Each development environment must be capable of invoking the runtime.

Possible future delivery methods:

- global CLI install,
- repo-local wrapper,
- package manager,
- agent-assisted bootstrap.

Exact installation is deferred to implementation design.

Once provisioned:

> The human never operates `agentctl` directly during normal project development.

---

## 36. Missing Runtime

If an incoming agent cannot invoke the runtime:

1. diagnose it,
2. use an approved bootstrap path if available,
3. avoid asking the user to type technical commands when the agent can perform them,
4. only surface the problem if the environment cannot be initialized automatically.

This is a setup failure, not a normal handoff flow.

---

## 37. Protocol Version

The repository should contain a minimal protocol version.

Example concept:

```json
{
  "protocol_version": 1
}
```

Purpose:

prevent an old runtime from silently misreading newer handoff state.

Detailed migrations are deferred.

---

## 38. Human Choice Presentation Rules

When a handoff-specific decision is required:

- prefer 2–3 options,
- describe outcomes, not tooling,
- explain meaningful risk,
- preserve state first where possible,
- never ask the user to execute commands.

Bad:

```text
Should I run git rebase --abort?
```

Good:

```text
A. 保留上一個 Agent 的未完成工作並重新整合
B. 回到最後已驗證 checkpoint
```

### Avoid Confirmation Spam

After the user chooses a safe option, execute it.

Do not repeatedly ask for confirmation unless an operation is unusually destructive and cannot first be made recoverable.

### Reversibility Principle

> **Preserve first; ask second; execute third.**

---

## 39. Core Agent Behavior Contract

### Development session start

```text
discover protocol
↓
resume
↓
establish ownership
↓
read active Plan / current Step
↓
work
↓
checkpoint after meaningful unit
```

### Normal stop

```text
preserve work
↓
checkpoint
↓
handoff
↓
release ownership
```

---

## 40. Read-Only Sessions

Not every interaction requires ownership.

Examples:

```text
"Explain the current plan."
"Review this code."
"What was implemented?"
```

Read-only inspection may proceed without write ownership.

Ownership is required before modifying shared project state.

---

## 41. User Says “Continue” While Another Writer Appears Active

If ownership clearly belongs to another still-active environment:

- do not write.

If v1 cannot determine whether that environment is still active because there is no heartbeat:

- present a simple handoff-specific choice,
- let the human say whether the previous environment has stopped,
- perform ownership recovery automatically if authorized.

Normal handoff avoids this situation by releasing ownership.

---

## 42. State Consistency Invariants

The runtime must not report the project as safely resumable unless:

- the referenced checkpoint exists remotely,
- the active Plan file exists if `active_plan` is set,
- the state file is structurally valid,
- ownership can be safely established.

If an invariant fails:

- do not guess,
- preserve state,
- perform deterministic repair when possible,
- otherwise request only a handoff-specific choice.

The user never edits state files manually.

---

## 43. Plan Mutation During Implementation

Plan changes are normal project work.

`agentctl` does not arbitrate them.

If the implementation reveals that the Plan should change:

- the coding agent discusses the project decision with the user if needed,
- edits the Plan,
- continues normally.

This is outside Handoff Decision logic.

---

## 44. CI / Tests / Verification

`agentctl` does not become a test orchestrator in v1.

Project instructions continue to define:

- tests,
- lint,
- type checking,
- builds,
- integration validation.

The coding agent uses those tools normally.

A checkpoint is not automatically equivalent to a verified checkpoint.

---

## 45. Plan Completion

When a feature is genuinely complete:

1. the coding agent performs project-appropriate verification,
2. Plan status changes from `READY` to `DONE`,
3. runtime clears `active_plan`,
4. the final checkpoint is pushed,
5. ownership may be released.

No next Plan is automatically activated.

---

## 46. Internal CLI Surface

Humans do not use these commands directly.

The internal API may conceptually expose:

```text
init
resume
status
start
checkpoint
handoff
done
recover-ownership
```

Exact names are not frozen here.

Because the interface is agent-facing, command count matters less than:

- deterministic semantics,
- structured output,
- safe defaults,
- hard-to-misuse transitions.

---

## 47. Machine-Readable Output

Commands that drive agent decisions should support structured output.

JSON is a likely option.

Human-formatted output can exist for diagnostics, but it is secondary.

---

## 48. Success Scenarios

### Scenario A — Normal Handoff

```text
Computer A

User:
今天先到這裡。

Agent A:
checkpoint
handoff
push
release ownership
```

Later:

```text
Computer B

User:
繼續開發。

Agent B:
resume
obtain ownership
load Plan/Step
continue
```

No technical human operation.

---

### Scenario B — Quota Exhaustion

```text
S1 complete → checkpoint
S2 complete → checkpoint
S3 partially implemented
↓
Agent A quota exhausted
```

Computer B:

```text
User:
繼續開發。
```

Agent B:

1. sees unreleased ownership,
2. cannot prove Agent A is inactive,
3. asks whether the previous environment has stopped,
4. user selects yes/no,
5. if yes, Agent B recovers ownership,
6. resumes from the latest shared checkpoint,
7. continues S3.

The uncheckpointed part of S3 may be lost.

This is an explicit v1 tradeoff.

---

### Scenario C — New Plan Exists but Is Not Active

```text
P-020 Home Automation
Status: READY
```

User says only:

```text
繼續開發
```

No active implementation exists.

Agent does not silently start P-020.

If user says:

```text
開始做之前規劃的 Home Automation。
```

Agent activates it and begins.

---

### Scenario D — Project Bug

During implementation, tests fail repeatedly.

The coding agent follows normal project debugging policy.

`agentctl` does not decide whether:

- the test is wrong,
- source code is wrong,
- architecture should change.

If the session ends, `agentctl` only transfers the opaque blocker summary.

---

### Scenario E — Handoff-Specific Divergence

Two development computers have incompatible continuation states.

The incoming agent:

1. preserves both,
2. determines automatic safe selection is impossible,
3. presents human-readable handoff choices,
4. user selects one,
5. agent performs the recovery,
6. development continues.

The human does not touch Git.

---

## 49. Resolved Ambiguities and Logic Conflicts

This section records issues discovered during design review.

### 49.1 Duplicate Plan Active State

**Problem:** Plan file and runtime could disagree.

**Resolution:**

```text
Plan durable status = DRAFT / READY / DONE
active Plan = runtime state only
```

---

### 49.2 Skill as the Only Bootstrap

**Problem:** New machine/provider may not have the Skill.

**Resolution:** Repository bootstrap is mandatory; Skill is optional reinforcement.

---

### 49.3 User Has to Manually Unlock

**Problem:** Violates invisible-control-plane UX.

**Resolution:** Agent asks only for handoff intent and executes recovery itself.

---

### 49.4 No Heartbeat Means Crash Cannot Always Be Proven

**Problem:** Incoming agent may not know whether the old writer is alive.

**Resolution:** Accept this v1 limitation. Ask one simple handoff-specific human question when necessary.

---

### 49.5 “Continue” with No Active Plan

**Problem:** Agent may opportunistically start a READY Plan.

**Resolution:** Do not auto-select a new Plan.

---

### 49.6 Plan Creation Accidentally Starts Implementation

**Problem:** Planning and execution become conflated.

**Resolution:** Creating a Plan does not activate it.

---

### 49.7 User Stops Mid-Step

**Problem:** Requiring completed Steps would leave valuable work only locally.

**Resolution:** Normal handoff can create a WIP checkpoint.

---

### 49.8 Checkpoint Means “Tests Passed”

**Problem:** WIP snapshots may be durable but not verified.

**Resolution:** Checkpoint means recoverable shared state only.

---

### 49.9 Bug Fuse Expands `agentctl` Scope

**Problem:** `agentctl` starts interpreting project bugs.

**Resolution:** Coding Agent owns bug policy; `agentctl` stores only opaque blocked state.

---

### 49.10 Every Git Conflict Becomes Handoff Logic

**Problem:** Normal project merges also conflict.

**Resolution:** Use the Handoff Boundary Test.

---

### 49.11 State Becomes a Growing Journal

**Problem:** Git/context noise grows indefinitely.

**Resolution:** Keep current state compact; Git history is the history.

---

### 49.12 Ownership Stored Only in Normal Git State

**Problem:** Live exclusivity cannot be reliably coordinated across machines.

**Resolution:** ownership is a separate remote coordination primitive.

---

### 49.13 Crash Recovery Pretends to Recover Unpushed Data

**Problem:** Remote machines cannot recover bytes never pushed.

**Resolution:** v1 guarantee ends at the latest shared checkpoint.

---

### 49.14 Decision-Only Intervention Expands into Project Decisions

**Problem:** The tool becomes a project manager.

**Resolution:** Decision-Only Human Intervention is strictly handoff-specific.

---

### 49.15 `active_plan` Exists but Plan Was Deleted or Renamed

**Problem:** state points to nonexistent intent.

**Resolution:** resume must fail safely, preserve state, and repair deterministically only when the intended Plan can be unambiguously located. Otherwise surface a handoff/state inconsistency; never guess a replacement Plan.

---

### 49.16 Plan Is Marked DONE While Runtime Still Points to It

**Problem:** duplicate transition partially succeeded.

**Resolution:** completion must be treated as one logical transition. Resume may repair only the unambiguous half-completed state, e.g. `DONE + active_plan=same plan` can safely clear the pointer after validating the completion checkpoint.

---

### 49.17 Two Agents Start at Nearly the Same Time

**Problem:** both may believe ownership is free.

**Resolution:** ownership acquisition must be atomic at the remote coordination layer. This is a hard implementation requirement even though exact Git plumbing is deferred.

---

### 49.18 Agent Creates a New Plan Without Ownership

**Problem:** plan IDs or files could race.

**Resolution:** any repository write, including Plan creation, requires ownership.

---

### 49.19 Read-Only Agent Accidentally Mutates State During Resume

**Problem:** merely asking for project status could seize ownership.

**Resolution:** protocol must distinguish read-only inspection from development resume. Ownership is acquired only before writes.

---

### 49.20 User Closes a Session Without Saying “Stop”

**Problem:** normal handoff cannot run.

**Resolution:** treat the next incoming session as abnormal handoff. v1 may require the user to state whether the old session has stopped. No hidden guarantee of graceful shutdown is assumed.

---

### 49.21 Handoff Summary Contradicts Git

**Problem:** natural-language summary may be stale or wrong.

**Resolution:** Git/code is authoritative. Handoff summary is guidance only. Incoming agent validates relevant state before trusting it.

---

### 49.22 Current Step Is Stale After Crash

**Problem:** state may say S3 while unpushed S3 work is gone.

**Resolution:** `current_step` is a cursor, not proof of completed work. Incoming agent resumes S3 from the latest shared checkpoint.

---

### 49.23 Multiple READY Plans Share Similar Names

**Problem:** natural-language feature request may match several Plans.

**Resolution:** coding agent asks ordinary intent clarification. Do not treat this as handoff ambiguity.

---

### 49.24 Ownership Recovery Could Destroy the Old Agent’s Future Push

**Problem:** old agent may resume after ownership was taken over.

**Resolution:** after ownership loss, an agent must revalidate ownership before any subsequent shared write/checkpoint. A stale former owner must be prevented from pushing through the handoff workflow.

This is a critical v1 invariant.

---

## 50. Critical Invariants

The following are non-negotiable.

### I1 — One writer

Only one current owner may perform handoff-managed writes.

### I2 — Atomic ownership acquisition

Two agents cannot successfully acquire ownership concurrently.

### I3 — Revalidate before shared write

An agent must not assume ownership remains valid forever.

Checkpoint/handoff operations must verify ownership before writing shared state.

### I4 — No duplicate source of truth

Plan status, active Plan, current Step, and ownership each have one canonical location.

### I5 — Never invent missing project intent

If Plan identity is ambiguous, do not guess.

### I6 — Preserve before destructive handoff recovery

When possible, make recovery choices reversible.

### I7 — Handoff summary is advisory

Code/Git state overrides narrative summary.

### I8 — User never operates recovery plumbing

Human chooses outcomes; Agent performs operations.

### I9 — Project semantics stay outside `agentctl`

No product/architecture/test correctness engine.

### I10 — Crash guarantee stops at shared state

Do not claim recovery of unpushed local data.

---

## 51. Intentional v1 Tradeoffs

These are deliberate simplifications, not bugs.

### 51.1 Small uncheckpointed work may be lost across machines

Mitigation:

- small Steps,
- frequent checkpoints.

### 51.2 Crash may leave ownership unreleased

Mitigation:

- incoming Agent requests a simple human declaration when needed,
- Agent performs the ownership recovery.

### 51.3 Plan scanning may become inefficient later

Accepted until real usage shows otherwise.

### 51.4 Current coding agent writes handoff summary

No separate summarizer service.

### 51.5 No autonomous project queue

Human intent starts the next Plan.

---

## 52. v1 Minimal Feature Set

The first implementation should provide only:

```text
Repository bootstrap
+
Single-writer remote ownership
+
Active Plan pointer
+
Current Step pointer
+
Checkpoint
+
Normal handoff
+
Resume
+
Minimal ownership recovery
```

Everything else is optional or deferred.

---

## 53. Product Invariant

The primary success criterion is:

> **A user can move to another configured development computer, open the same project, say “繼續開發”, and have the new coding agent safely continue the previous agent’s work without the user operating `agentctl` or Git.**

If a proposed feature does not materially support this invariant, it should not be part of v1 core.

---

## 54. Architectural Identity

The v1 product is:

> **A lightweight, Git-backed, agent-operated handoff control plane for sequential AI coding sessions across computers and providers.**

It is **not**:

> a multi-agent autonomous software engineering platform.

That distinction is intentional.

---

## 55. Deferred Implementation Specification

This Solution Design intentionally does not freeze:

- implementation language,
- exact command names,
- JSON schema,
- YAML schema,
- Git ref names,
- ownership compare-and-swap mechanism,
- package layout,
- installation mechanism,
- exact exit codes,
- structured output schema,
- Git plumbing commands,
- lock recovery internals,
- protocol migration internals,
- automated tests.

Those belong in the later Implementation Specification after the remaining design questions are resolved.

---

## 56. Recommended Next Brainstorm Topics

These are narrow enough to improve v1 without expanding scope.

### A. Ownership Semantics

Define:

- how ownership is acquired atomically,
- when it is revalidated,
- how it is released,
- how former owners are prevented from pushing after takeover.

### B. Checkpoint Semantics

Define:

- meaningful checkpoint threshold,
- WIP checkpoint behavior,
- commit/push model,
- how current Step advances.

### C. Plan File Contract

Define the minimum Plan fields required for reliable continuation.

### D. Bootstrap Reliability

Define how Codex, Claude, and future coding agents discover and invoke the runtime reliably.

### E. Abnormal Handoff Recovery

Define exactly what information the incoming agent presents when previous ownership was not cleanly released.

---

## 57. Version History

### v1.0-draft

This version deliberately simplifies the earlier v0.1 concept.

Key decisions:

- Product renamed/reframed as Agent Handoff Runtime.
- Human does not operate `agentctl`.
- Repository bootstrap is mandatory.
- Agent Skill is optional reinforcement.
- Strict Handoff Control Plane boundary.
- Decision-Only Human Intervention applies only to handoff ambiguity.
- Project bugs, tests, architecture, and product decisions remain outside `agentctl`.
- No Plan Registry in v1.
- No Epic hierarchy in v1.
- No autosave daemon in v1.
- No heartbeat daemon in v1.
- No semantic bug engine in v1.
- No automatic semantic Git conflict resolver in v1.
- Durable Plan status reduced to DRAFT / READY / DONE.
- Active Plan exists only in runtime state.
- Normal handoff permits WIP checkpoints.
- Crash recovery guarantee explicitly ends at the latest shared checkpoint.
- Remote ownership is the only required special live coordination mechanism.
- Ownership must be atomic and revalidated before shared writes.
- Stale former agents must be prevented from continuing to push after takeover.

---

## 58. Closing Scope Rule

When evaluating any future idea, ask:

> **Does this directly help one coding agent safely hand development work to another coding agent?**

If not:

> **It probably does not belong in Agent Handoff Runtime v1.**


---

## 59. Open-Source Template Secrets Integration

Decision recorded: 2026-10-02. Doppler is the recommended secrets provider for projects generated from this template. This section records a design decision; the template and runtime are not implemented yet.

### Template Contract

- Ship a documented Doppler setup example, a secret-name manifest, `.env.example` containing placeholders only, and ignore rules for local secrets, private keys, generated credential files, and machine-local provider settings.
- Store only non-sensitive provider selection, project/config references, required variable names, and validated relative file destinations in repository configuration. Do not ship author credentials or bind generated projects to the author's Doppler workplace.
- Each adopter supplies their own Doppler project/config. Each machine authenticates independently; initial browser login remains a human step. Authentication tokens stay outside the repository.
- Doppler is recommended, not required for read-only inspection or tests that need no secrets. Support an explicitly selected local-env mode; never silently fall back when Doppler fails.
- Keep integration in template setup and agent instructions. `agentctl` coordinates handoff and reports setup readiness; it does not become a secrets vault, upload service, or secret rotation engine.

### Agent Workflow

1. Inspect local changes and required configuration without printing secret values.
2. Validate the selected provider, project/config, CLI availability, and authentication. Missing setup produces a clear setup error; it does not prevent unrelated work that needs no secrets.
3. For commands that need secrets, use explicit project/config arguments and structured command arguments with `doppler run`; inject only into the required child process. Do not start the entire coding agent with application secrets.
4. If an application requires a credential file, create it with restrictive permissions in a validated local destination. Preserve existing files, stage replacements privately, and replace only after successful preparation. Do not automatically import binary certificate bundles as environment variables; their file delivery requires a separate documented mapping.
5. Report only configuration readiness and command outcomes. Do not include secret values in model context, checkpoint metadata, summaries, shell history, diagnostic dumps, or logs.

### Update and Publication Rules

- Doppler is the shared source for Doppler-mode secrets. Manual local `.env` or credential edits are preserved and are not automatically uploaded, overwritten, or merged into Doppler.
- Updating shared secrets requires an explicit user request specifying the intended project/config. Credentials must not be requested in chat; use the provider UI or an approved local input path.
- Checkpoint publication uses an explicit file selection and rejects known secret paths, including files already tracked by Git. Ignore rules alone are insufficient; these checks do not guarantee detection of arbitrary secrets embedded in source code.
- A missing or unavailable provider must not be reported as successful setup. Cache/offline behavior must be explicit, and cached values must not be presented as freshly fetched.
- Credentials are not part of the Git snapshot. A published code revision does not freeze secret values; record non-sensitive required names/config references and check compatibility on resume.

### Acceptance Criteria

A fresh adopter can configure their own Doppler project on Ubuntu; a second machine can authenticate independently and run secret-dependent commands. Missing CLI/login/config produces a setup error, ordinary inspection remains available, local edits are preserved, and credential files/tokens never enter checkpoints. Local-env mode works only when explicitly selected. Validate actual Doppler behavior during implementation; no integration tests have been run for this decision.

References: https://docs.doppler.com/docs/cli and https://docs.doppler.com/docs/create-project. Pricing and free-tier limits are external policy and must not be hard-coded into the runtime contract.