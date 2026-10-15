# A31 actual Claude takeover interaction

The [reviewed record](claude-takeover-evidence.json) proves one two-session interaction using exact `claude-haiku-5-5`, Claude Code 2.1.296 and an installed CLI whose 37 runtime modules match source. Both Agent sessions are fresh; the Git authority is an isolated local bare repository with a prior owner.

The first session reads the canonical instructions, inspects, creates private context and receives `decision_required`. Its actual prose explains takeover and abort effects and requests the user's choice. The harness supplies the prior public presentation, public decision envelope and an explicit simulated takeover selection to a separate fresh session. That session applies exactly the selected decision/option, resumes `ready/owned`, then edits managed `app.py` to return 42.

Primary reviewed both final prose responses. Actual proxy logs, native tool events and independent Git/capability checks prove ordering, a distinct new owner session, unchanged original source HEAD/index, unchanged mainline, and no feature publication. Private context contents are absent from both traces and stderr. The byte guard covers only Claude `settings.json` and `.credentials.json`.

The initial harness returned failure because its post-ready Python grammar rejected `print(app.answer())` and the explicit current-directory import setup. Focused regressions now accept these exact readonly AST forms. The retained native traces and final state were independently revalidated without another provider call; the record distinguishes that revalidation from an original exit-0 harness run.

Earlier failed interactions remain in the three `claude-takeover-*-failed-evidence.json` records. They prompted concrete context reuse, conditional decision-before-resume instructions and complete public conversation handoff. One earlier fixture exposed private context; its failure is not erased by this scoped successful run.

Reproduce with an authenticated Claude CLI and verified installed runtime:

```bash
python3 scripts/provider_interaction_smoke.py \
  --agentctl /path/to/installed/bin/agentctl \
  --claude /path/to/claude \
  --model claude-haiku-5-5
```

Raw traces and recovery state remain private outside the repository. The runner still requires Primary prose review after mechanical verification. This does not certify GitHub transport, other models/platforms, full host preservation, or all A54 privacy paths.
