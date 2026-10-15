# Design v1.2 completion audit

Verified 2026-10-15 for the **user-approved basic Doppler contract** on Python 3.12 / Ubuntu WSL. This explicitly replaces the earlier provider transaction-level CAS/fencing/terminal-API prerequisite. The remaining design scope is retained. Detailed requirements and historical evidence: [JSON audit](design-v1.2-completion-audit.json).

| §25 | Result | Evidence and boundary |
| --- | --- | --- |
| 1. A01–A63 | Proved for required scope | Six audits contain all 63 unique rows; no open classifications or missing referenced test functions. Current full suite:444 passed131.929s. |
| 2. Real Git hosting CAS | Proved for required scope | [GitHub evidence](hosting-cas-evidence.md): competing expected-revision writes, one publication/one rejection, unchanged mainline. |
| 3. Ubuntu/WSL | Proved for required scope | Current full suite and offline wheel/sdist installation/lifecycle/recovery/transition smokes passed; [release record](basic-release-verification.json). |
| 4. Fresh Agent bootstrap | Proved for required scope | [Actual confined Haiku feature turn](feature-workflow-evidence.json): canonical read, inspect, owned resume, managed edit and preservation. Existing proof rechecked; no new model turn. |
| 5. Isolated Doppler | Proved for basic contract | [Live evidence](doppler-basic-evidence.json): writes, conflict rejection, injected post-write acknowledgement loss, invalid-token denial and verified cleanup. No actual outage or scoped permission403 claim. |
| 6. Secret leakage | Proved for supported paths | Full local CLI/error/journal/Git leak tests, [model-bound error regression](a54-model-bound-error-verification.json), actual confined feature trace and live public-result scan. Historical failures retained; unsafe profiles disabled. |
| 7. Stale decisions | Proved for required scope | Takeover and integrity tests reject stale decisions without publication. |
| 8. Semantic conflicts | Proved for required scope | A32/A33 tests exclude project/product semantics from runtime decisions. |
| 9. Host preservation | Proved for required scope | Installed lifecycle/repair tests preserve selected Linux VPN/network/SSH/provider and other inspected categories; no global host or Windows-service certification. |
| 10. Unsupported boundaries | Proved for required scope | [Support matrix](support-capability-matrix.md) and negative tests enforce declared OS/Git/provider limits. Basic synchronization is explicitly documented. |

| Acceptance IDs | Detailed audit |
| --- | --- |
| A01–A10 | [Ownership/CAS](ownership-acceptance-audit.md) |
| A11–A20 | [Snapshot/recovery](snapshot-recovery-acceptance-audit.md) |
| A21–A30 | [Integrity/preservation](integrity-preservation-acceptance-audit.md) |
| A31–A40 | [Decision/Plan](decision-plan-acceptance-audit.md) |
| A41–A54 | [Secrets](secrets-acceptance-audit.md) |
| A55–A63 | [Security/bootstrap/handoff](security-bootstrap-handoff-acceptance-audit.md) |

Basic Doppler synchronization is per key, with expected-value preflight, readback and observation-based recovery without replay. It does not provide atomic same-key CAS or cross-service transactions. Unsupported native platforms and unsafe legacy model profiles remain unsupported. Final artifact hashes are in `dist/package-verification.json`; historical evidence is preserved separately.
