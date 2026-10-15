# A45 live Doppler remote-only delta

The [executed record](doppler-delta-evidence.json) covers one disposable ordinary key in the authorized `agent-handoff-test/dev` config, using Doppler CLI 3.77.0 and actual `SecretService` with a local bare Git authority.

The fixture first establishes equal local data and private baseline. An external CLI event changes only the remote value. At the local replacement hook, the runner verifies that a private backup already contains the original bytes and the local file still contains those bytes. After application and verification, local data and persisted baseline equal the changed remote value; the baseline identifies the actual verified operation.

Publication ref, owner, mainline, source HEAD and index remain unchanged. The runtime upload method is guarded and never called. The only external writes are fixture seed, remote change and deletion of the new random test key. Cleanup is acknowledged and a scoped read verifies absence. Existing keys are never selected for writes. Synthetic values are absent from the collected public runtime results; private recovery data stays outside the repository.

Reproduce with authenticated Doppler CLI:

```bash
python3 scripts/doppler_delta_smoke.py --project agent-handoff-test --config dev
```

Three focused offline driver checks cover destination restriction, timeout refusal of further writes, and the actual local service flow with mocked external events. The live run separately exercises the real provider. This evidence does not prove runtime upload fencing, provider timeout/permission behavior, PEM deltas, installed CLI behavior, or all A54 disclosure paths.
