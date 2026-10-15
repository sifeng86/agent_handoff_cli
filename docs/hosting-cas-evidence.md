# GitHub conditional publication evidence

Observed 2026-10-09 09:33 UTC using the user-authorized isolated remote `git@github.com:sifeng86/agent_handoff_cli.git`. The smoke harness first passed against a disposable local bare repository, then exited 0 against GitHub.

Reproduction creates a new randomly named test branch and retains it:

```bash
python3 scripts/hosting_cas_smoke.py \
  --authority git@github.com:sifeng86/agent_handoff_cli.git \
  --mainline-ref refs/heads/main \
  --base f95c4443da110f6dcb2acaef77264451a812befc
```

| Fact | Observed evidence |
| --- | --- |
| Test ref | refs/heads/agent-handoff-smoke-e918de48257545a18e688f5b578ed37c |
| Expected-absent initialization | published; e7b96c4af61f50c3e8711d956369a45320a11e67 |
| Writer A | rejected; candidate e4cf656b10e99832eb0d11af3e66dac6bcb1bcdc |
| Writer B | published; candidate d44dee3c622a5fb3a9fb4d2a4f82c88754237df6 |
| Final remote HEAD | d44dee3c622a5fb3a9fb4d2a4f82c88754237df6 |
| Winner parent | Exactly the initial revision |
| Mainline | Unchanged at the supplied base |

Both spawned processes use independent bare caches, observe the same initial HEAD, and prepare different commits before a shared barrier permits either push. Each sends once with the same explicit expected OID. Raw backend responses are captured and excluded from public output. The test branch is retained rather than force-deleted.

This proves expected-absent branch creation and competing GitBackend conditional pushes for this hosting configuration. It supports the CAS component of A02/A10 but does not yet prove their complete ownership/session scenarios. It does not establish hosting policy for every branch, runtime acquisition races, transport timeouts, crash recovery, or all Git acceptance cases.

The local harness exposed a simultaneous receive-pack rejection classified as unknown. GitBackend now treats explicit single-ref porcelain `[remote rejected]` as terminal rejection, while transport failure without terminal rejection remains unknown. A regression test covers both outcomes.
