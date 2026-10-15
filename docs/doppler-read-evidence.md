# Live Doppler scoped-read evidence

Observed 2026-10-09, using Doppler CLI v3.77.0 and locally authenticated credentials. User authorized CLI setup of the isolated test destination. Project `agent-handoff-test` and config `dev` already existed; missing sample key `RUNTIME_TEST` was created via stdin. No existing value was replaced. Credentials and values are absent from this evidence.

Reproduction from the repository:

```bash
python3 scripts/doppler_read_smoke.py --project agent-handoff-test --config dev --key RUNTIME_TEST
```

The persisted script exited 0 against the real Doppler destination. Its Git authorities are disposable local bare repositories, not GitHub.

| Scenario | Verified result |
| --- | --- |
| A41: absent local material and baseline, readable remote | Scoped remote read; required local material and private baseline created; subsequent convergence verified |
| A42: equal local/remote values, absent baseline | Scoped remote read; private baseline established and convergence verified |
| Public result privacy | Sample value absent from all captured configure/reconcile/apply/verify public results |

This establishes these readonly scenarios for one ordinary nonempty string key. It does not prove all A54 leak paths, restricted/empty values, file-backed material, backend permission/timeout semantics, conditional writes, takeover fencing, Dashboard concurrency, or the complete A41–A54 suite. Remote writes are disabled by the approved runtime binding in both scenarios.

## PEM material followup

On 2026-10-09, the same persisted script exited 0 for A41/A42 with isolated dummy key `RUNTIME_PEM` and mapping `keys/cert.pem`:

```bash
python3 scripts/doppler_read_smoke.py --project agent-handoff-test --config dev \
  --key RUNTIME_PEM --file-mapping keys/cert.pem
```

The key was created only after verifying absence, via stdin with captured responses. This run verified the real provider scoped read, private file content and baseline equality, dotenv pointer-only content, and absence of the PEM value in captured public results. The PEM is synthetic text for transfer testing; no certificate trust or cryptographic validity is asserted. Runtime remote writes were disabled. Partial file apply recovery, arbitrary editor races, and live provider write fencing remain unverified.
