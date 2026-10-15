# Live hosting Plan and takeover evidence

`scripts/hosting_plan_smoke.py` passed against the user-authorized GitHub SSH remote on 2026-10-13. The machine-readable [evidence record](hosting-plan-evidence.json) contains script/runtime hashes, exact publication revisions, Python/Git versions and an independent readonly confirmation. GitHub server version is unavailable and is recorded as null.

Reproduce with an explicitly observed current mainline SHA-1:

```bash
python3 scripts/hosting_plan_smoke.py \
  --authority git@github.com:sifeng86/agent_handoff_cli.git \
  --mainline-ref refs/heads/main \
  --base f95c4443da110f6dcb2acaef77264451a812befc
```

The script creates two independent no-checkout clones and one unique publication branch. Each successful publication is checked against its expected single parent and exact remote head. Session proof and published owner identity are checked after acquisition, takeover, checkpoint and done.

| Requirement | Executed evidence |
| --- | --- |
| A06 | Writer B receives an explicit takeover decision against writer A, applies it and proves its new capability/owner identity. |
| A07/A08 | Old writer A checkpoint and release fail fenced; remote head, retained edit, original HEAD and index bytes remain unchanged. |
| A11 | B handoff puts the exact selected payload, READY Plan, working cursor/summary and owner null in one commit. |
| A13 | B done marks the active Plan DONE and state idle with null Plan/step cursors in one commit. |
| A14, explicit false intent | done with release_intent=False retains the proved owner; a later release clears it. |
| A39 | A release preserves exact READY Plan bytes and working state/step while clearing ownership. |

Main remained `f95c4443da110f6dcb2acaef77264451a812befc`. The temporary branch was deleted using an exact final-OID force-with-lease; an independent readonly connection confirmed branch absence and unchanged main. The branch-cleanup local test moves the ref immediately before deletion and proves the competing ref survives lease rejection.

The focused local bare-Git fixture and cleanup race passed (two tests). The full socket-enabled suite passed 215 tests. Both refreshed wheel and sdist passed independent installed workflows and all 42 resource checks.

This is sequential post-takeover fencing evidence. It does not prove every in-flight writer race, snapshot restart/materialization on hosting, actual network timeout, every crash point, missing/true done release intent, fresh provider startup, Windows host preservation or Doppler conditional-write fencing. Those gates remain open; these rows are not a full v1.2 pass certificate.

A renewed run explicitly rejects reuse of the prior owner session ID and records `takeover_new_session=true`; see [ownership closure evidence](ownership-closure-evidence.json). That run independently confirmed unchanged mainline and branch cleanup. The historical record above remains unchanged.
