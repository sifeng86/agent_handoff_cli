# Secret operation recovery

`secrets resolve --operation-id ID` never resends a provider write or overwrites local material. It requires the operation's original private context, checkout identity and session acquisition history, plus the unchanged approved secret binding. A former owner can resolve historical evidence after release; this does not restore ownership or authorize further writes.

| Evidence | Result |
| --- | --- |
| Durable prepared operation, no send | Resolve local-only; reconcile fresh observations |
| Basic Doppler write with unavailable current read | Keep pending barrier and old baseline; report unknown |
| Basic Doppler current remote/local/pointers match the intended state | Resolve verified and advance baseline without replay |
| Basic Doppler current state differs or was only partly applied | Retain old baseline and private evidence; invalidate old plan/consent; reconcile fresh observations |
| Adapter with verified terminal receipts | Use those receipts before checking convergence; unknown receipts remain pending |
| Already resolved operation | Return historical recorded result without rewriting a newer baseline |

Prepared per-key records prove no send for that key. Basic Doppler confirmation observes current state and persists that observation; it does not claim a historical provider terminal outcome. Same-key concurrent edits have no atomic CAS guarantee. Adapters that supply terminal receipts retain their existing receipt-based resolution path.

Ordinary dotenv patches and file-backed local patches share the same durable local-patch journal. The intent is persisted before replacement, and a pending operation blocks another apply. Resolve only observes current material: it closes converged local work as local-only or divergent work as partial, without replay, rollback or a shared-baseline update. Local journal validation also rejects target changes outside the approved manifest scope while allowing unchanged extra local variables.

For a crash after a PEM file rename but before dotenv pointer replacement, resolve can close the operation without writing either file. A fresh reconciliation observes the retained PEM content, constructs a new private journal, installs the approved pointer and verifies convergence before advancing baseline. If local or remote observations conflict, fresh consent is required.

Resume does not transfer a checkout binding while a secret operation is pending, preserving the original caller proof needed for resolution. Git outcome uncertainty must also be resolved first. New synchronization continues to require current writer ownership.

Automated tests cover basic Doppler timeout confirmation after receipt loss, mismatch/baseline retention, no replay and fresh-plan recovery, plus partial PEM repair, released-owner resolution, foreign-context rejection and the resume barrier. Basic scoped Doppler uploads are implemented; actual provider acceptance is recorded separately from deterministic transport tests.

Ordinary local-patch tests additionally cover exceptions before/after rename and a real forked process exiting with code 77 after dotenv rename and parent-directory fsync. A new runtime resolves the retained journal without replacing the file; original Git HEAD/index/ref remain unchanged. This is process-crash evidence in WSL/Linux, without a physical power-loss or Windows-native claim.

## Local PEM patches

`secrets apply --patch-file /absolute/private/patch.json` accepts the existing
dotenv patch fields plus a paired `expected_files` / `file_changes` object.
Both objects use approved file-backed key names, with identical key sets.
Values are PEM contents or `null` for absent material; paths come exclusively
from the approved manifest. Keep the patch outside the checkout in a private
directory with mode 0700 and the patch file with mode 0600.

`expected` remains the complete observed dotenv key/value map. `changes` is
limited to ordinary dotenv keys. For file-backed keys, the runtime generates
the approved dotenv path when material is present and removes it on deletion.
Every selected null target requires its key in `approved_deletions`.

All PEM targets, mappings and observations are checked before mutation. The
private journal stores original dotenv text and selected original material
before the first write; individual replacements retain private backups too.
Multiple files and dotenv are separate atomic replacements, not one filesystem
transaction. An interrupted operation blocks further mutation until
`secrets resolve --operation-id <secret_operation_id>` inspects its evidence.
Resolve never replays writes or rolls back editor changes. A nonconverged local
operation closes as partial; build a fresh private patch from current values.
Local operations never update a shared baseline or claim `secrets_shared`.

