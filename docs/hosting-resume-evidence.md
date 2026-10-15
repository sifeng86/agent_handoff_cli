# Live hosting snapshot recovery evidence

`scripts/hosting_resume_smoke.py` passed on the user-authorized GitHub SSH remote on 2026-10-13. Each context/init/acquire/checkpoint/resume/recover-session/release phase starts a separate Python CLI process. Its public result is validated and its exact exit status checked. The [machine-readable evidence](hosting-resume-evidence.json) retains source hashes, exact revisions, versions, statuses and an independent readonly confirmation; no context token, local path, file contents or secret material is published.

Reproduce against an explicitly observed current mainline SHA-1:

```bash
python3 scripts/hosting_resume_smoke.py \
  --authority git@github.com:sifeng86/agent_handoff_cli.git \
  --mainline-ref refs/heads/main \
  --base f95c4443da110f6dcb2acaef77264451a812befc
```

| Requirement | Executed evidence |
| --- | --- |
| A12 | Checkpoint retains the proved owner and publishes selected text/binary plus READY Plan/working S-001 state. A new CLI process materializes those exact bytes at the checkpoint revision. New recover-session and clean managed resume processes prove the same private session without advancing the ref. |
| A25 | Source staged/unstaged edits remain integration-required: source resume exits 6 with not_ready/owned. Original HEAD/index/status and selected file/Plan/ignore-rule content hashes remain unchanged through resume, old-checkout rejection and managed release. |
| A26 | Post-checkpoint untracked binary and ignored dummy local file remain only in the source checkout, unchanged and absent from the managed snapshot. |
| Checkout binding | Recover-session on the original checkout exits 4/rejected after binding transfer. Managed checkout recovers successfully and repeated clean resume is ready at the same path/revision. |

Final release clears owner in a single-parent commit. Main remained `f95c4443da110f6dcb2acaef77264451a812befc`; the unique test branch was deleted using an exact final-OID lease. A separate readonly connection confirmed unchanged main and branch absence.

The local bare-Git fixture passed, followed by the full socket-enabled 216-test suite. This evidence covers separate CLI processes on the current Linux environment. Fresh AI provider bootstrap, independently fresh Ubuntu/WSL, semantic integration of preserved edits, actual transport faults, in-flight takeover and Doppler write fencing remain separate gates.
