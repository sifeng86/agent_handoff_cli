# Live GitHub ownership and crash evidence

Observed 2026-10-09 on the user-authorized remote `git@github.com:sifeng86/agent_handoff_cli.git`, using `scripts/hosting_runtime_smoke.py`. The local harness passed before the hosting run. The hosting run exited 0; an additional readonly check confirmed both releases changed only `.agent/owner.json` and final owner is null.

Reproduction creates a new isolated branch:

```bash
python3 scripts/hosting_runtime_smoke.py \
  --authority git@github.com:sifeng86/agent_handoff_cli.git \
  --mainline-ref refs/heads/main \
  --base f95c4443da110f6dcb2acaef77264451a812befc
```

| Scenario | Hosting evidence |
| --- | --- |
| A02 | Two independent clones/processes prepare runtime acquisition before either sends; writer A rejected/fenced, writer B published/owned |
| A04 | Winning local capability recovers against the same remote session; crash-reconstructed capability also recovers |
| A09 | Both owner releases succeed; readonly tree diffs prove only owner record changed; final owner null |
| A16 | Actual process exits 77 after server acceptance and before client reply handling; exactly one durable pending operation; restart resolves published and recovers ownership; remote HEAD unchanged through resolve/recover |

Retained ref: `refs/heads/agent-handoff-runtime-smoke-72b3c3e43d0c43dca0fdcb6a3b6d79ec`.

| Revision | OID |
| --- | --- |
| Expected-absent initialization | f938f2d0b719a57f791a564cf6b1af7a322d4d72 |
| Winning acquisition, exact initial parent | 25ba0e9133e3c21bbf8c9ca34d203eae0c208e70 |
| Accepted acquisition before crash | 4ce15935c478b1bab1b5d137c0fd804a87ad670e |
| Final released revision | b891f71e0c47b6b7b85e62ee7d6ab656327daa57 |
| Mainline before/after | f95c4443da110f6dcb2acaef77264451a812befc |

The fixture starts with idle state. This is not evidence for every working Plan state, handoff/done behavior, hosting takeover, every crash point, transport timeout or branch-protection policy. The injected crash follows a successful server reply at the subprocess boundary; it models loss before runtime processes that reply, not an actual network packet drop. No remote commands or checked-out repository scripts are executed; clone uses `--no-checkout`, explicit destinations and sanitized Git environment. Public output excludes private context values and raw backend responses.

## Expanded snapshot and release-crash run

Observed 2026-10-09 using the expanded persisted script. Local fixture and
real GitHub runs passed. Clone now selects the explicitly supplied mainline,
so the remote default HEAD cannot choose an unrelated or unborn source HEAD.

| Evidence | GitHub observation |
| --- | --- |
| Isolated ref | `refs/heads/agent-handoff-runtime-smoke-9ea762de17c2401282cff52b9a8c8541` |
| Initial revision | `608f75d2b7ac5427ee7e9e1befc38e0581d7480d` |
| Acquisition race winner | `3e785a1aee2ac3ddb9297d53027521054889cd31` |
| Crash-accepted acquisition | `b838a4f3444fed2c2d91c5943be0db5725074ac7` |
| Fixed checkpoint snapshot | `2161dc8b4676c68e397a00895d93cfb76330b028` |
| Crash-accepted release | `43b1e23bebf0490e69b0bd22709423d0de5d53ce` |
| Main before/after | `f95c4443da110f6dcb2acaef77264451a812befc` |

A15: the checkpoint publishes the frozen selected file while a hook edits its
source immediately before send. The source retains the editor change, its
HEAD and original index bytes/absence; the runtime does not relabel the new
edit as published. Local tests separately cover both checkpoint and handoff,
including staged index preservation and new untracked binary retention.

A20: a separate real process releases the recovered owner and exits 77 before
client reply handling. Its durable sending journal blocks recover-session.
Restart resolves the exact accepted commit twice with push disabled in the
resolver, leaves HEAD unchanged, clears pending intent and durably revokes the
old session. The release tree differs from the checkpoint only in owner.json.

This run also repeats A02/A04/A09/A16. Both crashes are injected at the runtime
reply-processing boundary after actual GitHub writes. Actual network packet
loss, delayed server execution, all working-Plan states and hosting takeover
remain separate acceptance gaps. The isolated branch remains for inspection;
main was not modified.
