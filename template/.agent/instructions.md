# Canonical Agent Handoff instructions

1. Discover `agentctl` and the committed protocol/config without modifying the project.
2. Use the explicitly approved authority and full publication ref. Do not guess `origin`, hosting aliases, or credentials.
3. Run `agentctl inspect` or `agentctl status` in a separate completed readonly tool call before `agentctl resume` and before the first project mutation. Reading instructions or a clean checkout does not replace runtime inspection or grant ownership.
4. If the user explicitly supplies the path to an existing private context file, reuse that exact path and skip context creation. Never open, read, or print the context file or its contents. Otherwise, create one explicit private runtime context outside every project checkout with `agentctl context create --checkout <original-checkout> --private-root <approved-private-root>` and use the reported context file path.
5. If the conversation already includes a pending decision and the user's explicit selection, apply that decision using step 7 before another resume. Otherwise, run `agentctl resume --authority <approved-authority> --publication-ref <approved-full-ref> --checkout <original-checkout> --private-root <approved-private-root> --context-file <reported-context-file>`. A `decision_required` result means stop feature work, present the offered control-plane choices, and wait for the user's selection. A user-approved control-plane decision may be applied without `ready`; only `ready` with proven ownership permits managed project work. Use the reported managed checkout for subsequent commands.
6. If local integration is required, the Coding Agent compares and integrates preserved original edits into the managed checkout. Do not reset or clean the original.
7. After the user selects an offered option, apply that exact pending decision with `agentctl decision apply --authority <approved-authority> --publication-ref <approved-full-ref> --checkout <original-checkout> --private-root <approved-private-root> --context-file <reported-context-file> --decision-id <pending-decision-id> --option-id <selected-option-id>`. Then run `agentctl resume` with the same authority, ref, checkout, private root, and context file to obtain `ready` with proven ownership before project work. If the decision is stale, stop and present the newly observed decision; never approve it automatically.
8. Use explicit file selections, summaries, Plan/Step state, and `done` release intention. Preserve edits made after snapshot collection.
9. Resolve pending operation IDs before subsequent mutations. Never blindly repeat ownership-changing intent after timeout/crash.
10. Keep feature/design/test semantic decisions in the ordinary coding workflow, outside the runtime decision engine.
11. Treat Git code publication and shared secrets as separate results. Basic Doppler per-key synchronization is supported only for the approved binding and explicit upload grant. It checks current ownership and remote values before writing, verifies readback, and resolves uncertain outcomes by observing current state without replay. It does not provide atomic same-key CAS or cross-service fencing; avoid concurrent edits to the same key.
12. Never expose dotenv values, provider tokens, private capability records, or secret fingerprints in model-visible output. Inspect reports key availability only.
13. If an approved secret binding exists or the project manifest declares required secrets, check local availability before starting any dependent runtime or test. A resume result that explicitly lists missing required keys already blocks those commands. Otherwise use `agentctl secrets verify --authority <approved-authority> --publication-ref <approved-full-ref> --checkout <reported-managed-checkout> --private-root <approved-private-root> --context-file <reported-context-file>`. Reuse the existing approved binding; do not guess its mode, project, config, or manifest. Dependent local work requires valid `availability.status: completed` and an empty `availability.required_missing`. If local material is missing, unavailable, or cannot be safely verified, stop the dependent runtime/tests. When local availability is verified and only shared convergence/read is unavailable, suitable local work can continue with `secrets_shared=false`; do not claim sharing succeeded. Continue independent work subject to the Git ownership and integration gates. Never guess values, insert placeholders, read `.env` or private context contents, or print secret values to model-visible output. Git `ready`/`owned` does not prove required secret availability.

If setup/authentication is unavailable, preserve all host settings and project work. If human OAuth/2FA is required, ask the user only to complete the external authentication step; the Agent remains responsible for `agentctl` and Git recovery and must not ask the user to operate the engine.

## User-facing onboarding report

For an install/adopt-only request, complete setup, preserve existing work, save and read back initial progress, and verify the handoff has released ownership before reporting success. Stop after reporting; feature development requires the user's next instruction. Do not repeat a possibly accepted write to obtain evidence; resolve its recorded operation first. Do not require a model API turn for onboarding verification.

Use these exact Traditional Chinese labels and their order. These are presentation labels, not new engine statuses. Fill only values supported by actual observations. Use `已完成` only if all setup, preservation, saved-progress/readback and released-ownership checks passed. Use `待確認` for missing authentication, permission or user information; use `未完成` for errors or uncertain outcomes. Never equate a copied template, a prepared plan, or an unverified write with completion.

<!-- onboarding-report:start -->
```text
導入結果：{已完成／待確認／未完成}
專案名稱：{專案名稱}
專案位置：{使用者提供的專案路徑}
專案狀態：{全新專案／既有專案}
原有工作：{已保留／未確認／不適用（全新專案）}
交接設定：{已建立／未完成}
初始進度：{已保存／未保存}
交接驗證：{通過／未驗證／未通過}
目前工作：{已停止，等待下一個指示／已停止，等待處理問題}
```
<!-- onboarding-report:end -->

For a blocked or unsuccessful outcome, append `原因：…` and `需要你處理：…` with a safe specific explanation and next action. Keep secret values, capability records and raw provider errors out of this report. Do not replace the result with vague synonyms such as `基本完成`. The tool repository's `docs/user-guide.zh-TW.md` is the canonical human input/report reference; keep its report block identical to this one when editing either file.
