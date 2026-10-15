# Basic secrets checkpoint — 2026-10-14

Historical checkpoint, superseded by the [verified basic release](basic-release-verification.json) on 2026-10-15. The remaining steps listed below were completed in that release.

Status: implementation and focused local tests passed; paused at the user's requested checkpoint. Full basic-version delivery is not yet verified.

## Confirmed product scope

Use ordinary synchronous Doppler key writes and current-state confirmation. Provider leases, atomic compare-and-set and durable terminal-operation APIs are not prerequisites. Git ownership, approved destination/key scope, explicit upload grant and private journaling remain enforced. Same-key concurrent writes have no atomic CAS guarantee.

User-facing outcomes remain the existing basic statuses: completed, rejected, unknown; a prepared operation is pending until run. No new CLI command or configuration format is introduced.

## Completed

- GPT-6-Luna implemented `src/agent_handoff/secrets/doppler.py` and `tests/test_doppler_basic_write.py`; Primary reviewed their scope and CLI flags.
- Key set/delete uses one explicitly scoped CLI invocation, with secret values supplied through stdin. No automatic retry; nonzero/timeout remains unknown. Read-before-write rejects an observed change. CR-containing values are explicitly unsupported by this stdin path; LF transport handling is unit-tested.
- Primary integrated `src/agent_handoff/secrets/service.py`: recheck ownership before each basic send; resolve observes current shared/local state without replay or provider status lookup. Baseline advances only after the existing convergence checks.
- Updated `tests/test_secret_service.py`: basic upload grant, different-key merge, timeout confirmation after loss of in-memory receipts, mismatch preserving baseline and allowing a fresh plan. Unimplemented adapters still reject upload grants.
- Focused command: `PYTHONPATH=src:tests python3 -m unittest test_doppler_basic_write test_secret_service test_secret_leak_matrix -q`; **65 tests passed in 30.168s**. Worker adapter-only run: 7 passed. Installed `doppler secrets set --help` confirmed stdin and the selected flags without accessing secrets.

## Next incomplete steps

1. Validate actual Doppler set/delete, literal value transport and A44/A46 merge in the isolated `agent-handoff-test/dev` environment. Preserve existing secrets; use dedicated disposable keys and private evidence. No real write or paid model call occurred at this checkpoint. Any sandbox escalation needs explicit approval under the current AGENTS.md.
2. Review current basic ownership/recovery behavior, and update the old strict-writer acceptance/support audits. The old vendor-contract requirement is superseded by the user's design decision; do not wait for vendor answers or claim that fake/local tests establish live Doppler behavior.
3. Run one full suite after the complete integration batch, then build/install the final wheel/sdist and verify current bytes. Current `dist/` artifacts predate these runtime changes; historical 425-test evidence is not verification of the new basic version.
4. Update remaining A44/A46/A54 and section-25 evidence honestly; audit the user's revised whole-project scope before declaring delivery complete.

## Resume

Read `.agent-state.md`, `git status -s`, and `git diff --stat`; the source tree is still untracked, so an empty diff does not prove no edits. No active worker or test process. Do not repeat the passing focused batch unless a new change or failure warrants it. No model API permission remains from earlier single-turn grants; no real-account credential restore is authorized.
