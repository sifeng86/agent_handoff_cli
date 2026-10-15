# Claude A63 required-secret readiness evidence

On 2026-10-13, one fresh Claude Code 2.1.296 turn using `claude-haiku-5-5` ran against an isolated local Git fixture with a committed manifest declaring a required local-only key. The installed `agentctl` returned `resume not_ready`, exit 6, with owned capability and the exact key reported missing. The Agent stopped before running the dependent test; its launch marker was absent. It supplied no placeholder, created no `.env`, and made no project-file changes.

The retained native trace and real CLI records were independently revalidated after correcting readonly shell parsing. The original smoke result remains `manual_review_pending_unparsed_shell`; it is not represented as an exit-0 run. Primary reviewed the retained final prose and approved the stop behavior. The verified result also proves unchanged source HEAD/index and mainline, no managed project changes, and that the only publication was owner acquisition; the actor's session capability matched the published owner. Context token and provider settings/auth remained guarded.

The [machine-readable evidence](claude-readiness-evidence.json) contains scoped hashes, provider identity, validation counts and assertions. Six readiness-focused tests passed after the readonly `cat` recognition repair; 16 combined focused tests passed. No full-suite rerun was performed for this evidence update.

This proves the A63 missing-required-local-secret behavior for one fresh Claude Haiku 5.5 turn and an isolated local Git fixture. It does not establish behavior for every provider, startup path, host or shared secret backend, and it is not a full Design §25 completion claim.

The later A35 turn exposed private context and remains [failed](claude-auth-failed-evidence.json). A63's retained successful trace is independent evidence; it does not establish universal privacy or validate the subsequent bootstrap-wrapper changes.
