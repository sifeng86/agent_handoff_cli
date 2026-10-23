# Package verification

Latest review: [2026-10-15 comprehensive verification](comprehensive-review-2026-10-15.json) records 446 passing tests, new wheel/sdist installation smokes, and all seven tutorial blocks executed against both installed CLIs. Current runtime/template fixes are included in the delivered artifacts; final documentation-only source refresh was byte-compared. Hashes: `dist/package-verification.json`. Earlier release narratives below are historical.

Current basic release: [444 passing tests and installed-package evidence](basic-release-verification.json), plus [real isolated Doppler acceptance](doppler-basic-evidence.json). Current wheel and source installation smokes passed. Final documentation-only source refresh preserves every verified runtime/helper/test/schema/template byte; delivery hashes are external in `dist/package-verification.json`. The earlier batch narratives below are historical, including their then-open gates.

`scripts/package_smoke.py` builds a staged source tree using the pinned
setuptools 75.8.0, then installs the wheel and source archive into two separate
venvs without network access or runtime dependency installation. The source
archive installation uses the supplied build tooling with build isolation
disabled; this verifies the source distribution without depending on PyPI.

Run from WSL Ubuntu with Python 3.12 and Git:

```bash
python3 -m pip install --target /tmp/agent-handoff-build-tools \
  setuptools==75.8.0 wheel==0.45.1
python3 scripts/package_smoke.py \
  --build-tools /tmp/agent-handoff-build-tools
```

The script keeps artifacts and private build logs in its reported temporary
directory. It verifies:

- All Python modules and all 42 schemas/template assets in both archives match
  the staged source bytes, including hidden `.agent` files.
- Installed resources match source bytes and imports originate from the venv.
- The installed `agentctl` runs init, acquire, recover-session and release from
  outside the repository without `PYTHONPATH` or an editable installation.
- Recovery does not publish, ownership is proven and released, final owner is
  null, and main plus the original source file/index remain unchanged.

The historical A31/A45 batch passed 268 consolidated tests and both installation routes under Ubuntu/WSL, with all 42 assets checked per route. Three focused tests subsequently verified unit-test evidence-pointer isolation; no runtime change or full-suite rerun. The staged artifact snapshot includes the batch evidence; later workspace delivery records summarize this completed build. The latest delivered artifacts and SHA-256
checksums are recorded in `dist/package-verification.json` in the workspace.
This is package installation evidence, not proof of the remaining provider,
fresh-agent, host-policy or full v1.2 acceptance requirements.

Package smoke also inspects selected Linux host configuration before builds/installations and compares it afterwards. Its report identifies preserved and unverifiable categories separately; see [host preservation](host-preservation.md). This does not prove live service or Windows host preservation.

Installed extended workflows run from external CWD with no source PYTHONPATH in both wheel and sdist venvs. Host backup/verify-backup/restore/resolve checks byte/mode/mtime recovery and public JSON field allowlist plus absence of raw/base64 material, file path and credential-derived digest. Authority proposal requires decision exit code 3; explicit apply publishes no revision, source acquire becomes fenced, and target independently initializes/acquires/releases. Both owners end null; source publication/main/index/code remain unchanged by the transition. These are local installed-CLI fixtures, not fresh external provider or full host/service evidence.

The latest A17/A58 batch ran the complete 311-test scope once: 310 test methods passed and one legacy mock fixture failed in two subtests because it used a placeholder OID. The corrected fixture and its 11-test module then passed without a full rerun. This is combined-scope verification, not a single exit-zero full-suite run; see [batch record](a17-a58-batch-verification.json). New wheel/sdist installations passed, and Primary verified all 38 runtime modules and 42 resource assets against current source. The sdist was refreshed only for that test fixture after installation; archive comparison proves every other file remained identical, so installed runtime/resources are unchanged. [Installed A58 SDK evidence](a58-installed-sdk-evidence.json) verifies backup-before-replace, exact launcher recovery and post-repair CLI preservation; no repair CLI subcommand or comprehensive host-state proof is claimed. Final delivery documentation postdates the artifact snapshot.

The provider-isolation/A54 batch subsequently covered 371 tests: the full run passed 368 methods and failed three multiprocessing fixtures because its stdin runner was not importable by spawn. Only those three methods were rerun through the standard unittest entry and passed (2.186s), without source changes or another full run. This is combined-scope evidence, not a single exit-zero full suite. See [provider batch](provider-isolation-batch-verification.json). The one authorized exclusive-MCP Haiku turn failed before any successful runtime operation; [failure evidence](claude-mcp-auth-failed-evidence.json) remains separate from local test results.

Both provider-isolation batch installation routes passed. Primary checked all 38 runtime modules and 42 resource assets in both archives against current source, plus all 70 Python scripts/tests in the sdist, and verified delivered artifact hashes. The wheel provides the runtime SDK/CLI; provider acceptance scripts are sdist helpers. Selected Linux host entries remained unchanged during this offline packaging run; services/users-security remain unverifiable. This host proof begins after the earlier Claude turn and does not negate its credentials-file change. Final delivery/audit documentation may postdate the staged artifacts.

After the second authorized model turn exposed the skipped inspect/status step, canonical instructions were clarified without relaxing acceptance. The six affected test modules passed 89 tests, and the actual offline engine smoke passed; the complete suite was not repeated. Final wheel/sdist installations include this changed template and both passed. Primary verified current bytes and hashes again; that historical delivery predates the third-turn auth proof below.

The latest [auth/support batch](auth-support-batch-verification.json) passed 381 tests in 113.293 seconds with the normal unittest module entry. An initial fixture failure caused by umask was corrected with explicit test-directory permissions and remains recorded. Both wheel and sdist installation routes passed, and Primary compared current bytes for 38 runtime modules, 42 assets and 71 Python helpers, then verified delivered hashes. Prior artifacts remain in `dist/previous-auth-support`. The third fresh Haiku readonly auth handoff is independently proven in its scoped evidence; A44/A46/A54 and broader host/provider gates remain open. Final audit/delivery documents postdate the staged archives.

The later [legacy provider guard](legacy-provider-guard-verification.json) changed smoke helpers only. Four affected modules passed 53 tests; no full-suite or installed-runtime rerun was needed. The sdist was rebuilt offline and all 71 helpers, 38 runtime modules and 42 assets compared with current source; the previously verified wheel is unchanged. This source-archive refresh includes the fail-closed legacy launcher fix. A confined feature-edit replacement remains required.

That replacement is now implemented and has an approved actual Haiku feature-turn record. The current [feature integration report](feature-integration-verification.json) records 425 tests passing in 121.451 seconds, plus real-engine/local-Git controller tests with multiple edits and journal checks. Wheel and source installations passed lifecycle, host recovery and authority transition smokes. The delivered source archive contains the feature helpers and tests; a final documentation refresh was checked to preserve every installed runtime/helper/test/schema/template byte. The runtime wheel is unchanged. Original provider evidence passed the stricter controller offline without another API turn. A44/A46 and remaining A54 external failure coverage still prevent full v1.2 completion.
