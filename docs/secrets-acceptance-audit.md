# Secrets acceptance audit (A41–A54)

Reviewed 2026-10-15 under the user-approved basic Doppler contract. Detailed test assertions and historical evidence remain in [the JSON inventory](secrets-acceptance-audit.json).

| IDs | Classification | Evidence |
| --- | --- | --- |
| A41–A42 | proven_hosting | Existing real isolated Doppler scoped reads/bootstrap/baseline. |
| A43 | proven_local | Initial conflict requires a decision without writing. |
| A44 | proven_hosting | Actual single-key upload, multiline/trailing-LF readback and baseline verified. |
| A45 | proven_hosting | Existing actual remote-only delta, local backup and baseline verification. |
| A46 | proven_hosting | Actual different-key merge uploads only A, adopts B, retains local-only material and verifies baseline. |
| A47–A50 | proven_local | Conflict privacy, deletion decisions and stale observations covered by runtime tests. |
| A51 | proven_local | Old ownership is rejected before sending; basic mode does not claim server-side atomic fencing. |
| A52 | proven_hosting | Actual write followed by injected acknowledgement loss returns unknown; observation resolves without replay. Local tests cover partial/mismatched outcomes and retained baseline. |
| A53 | proven_local | Injected unavailable provider preserves usable local material and safe failure output. Actual invalid-token read denial separately passed. |
| A54 | proven_hosting | Local leak matrix, confined broker error-boundary regression, retained successful confined Haiku trace, and current live public-result scan cover the supported paths. |

[Current live evidence](doppler-basic-evidence.json) records three runtime uploads, five fixture writes and two verified cleanup deletions. Only two random keys in `agent-handoff-test/dev` were used. Git source/index/ref/owner/mainline remained unchanged.

The timeout case injects acknowledgement loss after actual successful provider execution; it is not an actual network outage. Invalid-token authentication denial is verified, not a scoped permission403. Basic synchronization uses preflight/readback and current-state confirmation, without atomic same-key CAS or cross-service fencing.

A54 combines [local disclosure tests](../tests/test_secret_leak_matrix.py), [model-bound synthetic error evidence](a54-model-bound-error-verification.json), [actual confined feature evidence](feature-workflow-evidence.json), and current live output checks. Historical failed native-tool traces remain retained; their launch paths are disabled. Private recovery material intentionally contains secrets and is outside public output. No new model API turn was run.
