# Authority binding transition contract

狀態：durable selection、Runtime barrier、雙端 proposal/apply executor 與 CLI 已實作；三份 private record schemas 已加入，完整 concurrent/fault matrix 未完成，不是完整驗收通過的功能。本文件落實 Design v1.2 §5.1、§7、§11 與 spec §3，沒有變更 publication CAS 或 secret backend gates。

## Transition 的語義

Authority 是明確 destination/ref 的配對。無 backend immutable repository identity proof 時，即使 SSH/HTTPS URL 指向相同名稱，也視為不同 identity。使用者可明確核准改用另一個配對；核准不證明 alias 等價，不搬移 session，也不搬移 secret grant。

Transition 是同一 caller context 與原 checkout 對新 authority 的明確本機選擇。它不遷移遠端歷史、不改寫兩端 config、不 release 遠端 owner、不 acquire 新 owner。完成後，新端需要獨立 acquire/recover/takeover；若 config 與選定配對不一致，新端必須先經正常 init/integrity 流程，不可藉 transition 採納 invalid snapshot。

原 authority 的 private namespace 保留，供 resolve、audit 與其他 checkout 使用。新的 namespace 保存自己的 binding/trust/session/journals/baselines。不得複製 capability、checkout binding、Doppler binding、baseline、persistent upload grant 或尚未執行的 decisions。

## 前置条件與 observations

Propose 要求 source binding valid、明確 source/target destinations、不同 identities、明確 caller context，以及原 checkout 的 preservation record。Source 與 target 都讀取 exact remote HEAD，驗證 protocol/config/Plans；target 已存在時必須符合 target authority。Target absent 可以核准選擇，但不建立 ref、不建立 trusted HEAD，後續 init 仍使用 expected-absent CAS。

Source integrity violation 不可用 binding transition 繞過：先完成 integrity decision，或保留 abort-only decision。兩端未決 Git operation、shared/local secret operation、active unreleased checkout capability，都使 switch option 不可用。Source remote owner 可以是其他 session；它不授權 caller 變更 source。Target remote owner 可以非 null；切換仍不產生 writer。

先取得 caller selection lock，再對兩端 identity 依字典序取得 repository locks，接著取得 checkout lock。相同 identity 輸入直接拒絕。所有 transition-aware mutations 在一般 repo/session/checkout locks 前取得相同 selection lock，避免反向鎖序。Locks 只證明本機 serialization，不能宣稱跨 authority 的 remote atomicity。

## Immutable decision 與 private evidence

Public envelope 使用 category `authority_binding`；repository_identity/publication_ref/observed_head/observed_owner_session 指向 source，destination_identity 指向 target。Options 為 switch/abort；公開 effect 明列 target destination/ref、target observed HEAD/owner，以及 sessions/secrets 不移轉。不得僅顯示 opaque digest 讓使用者盲目批准。

Private evidence 綁 decision ID、source/target 配對及 identities、兩端 exact HEAD/owner、兩端 binding/trust/secret-binding dependency digests、caller context/checkout identities、preservation handle。Absent records 與 null HEAD 是明確狀態，不以 missing key 代替。嚴格驗證 schema、filename linkage、canonical paths、public/private agreement 與 preservation record。Raw provider errors、credentials、secret values 不進公開 decision。

Apply 在任何 write 前重新驗證 evidence、preservation、dependencies、兩端 pending barriers/capabilities 與 exact observations。任一改變使 decision terminal stale，要求重新 propose。Target HEAD/owner 也屬 stale precondition，不能只使用既有 Decisions.check 的 source 檢查。Abort 不改 selection、trust、binding 或 remote refs。

## Durable local commit 與 recovery

為 caller context/checkout 新增 private authority selection record；既有未登記 checkout 首次 mutation 只能在來源 observation/binding 驗證後登記。Read-only inspection 可指定其他 authority，但不更新 selection。Mutation 指向不同 authority 時要求 transition decision，不能靠改 CLI flags 自動核准。

Apply 使用 durable transition journal：prepared → selection_committed → resolved。Journal 必須先於 selection write fsync，包含唯一 transition ID、decision ID、前後 selection 與 dependency evidence。Selection atomic replacement 包含 transition ID，是本機 commit point。之後才把 decision 標 applied、journal 標 resolved。Transition 不寫入 target binding/trust。新 target namespace 在 selection commit 後由獨立 init 建立 binding/trust；existing target 也先 init 再 acquire。此流程避免 journal 前的部分 bootstrap effects，且不把 target 初始 HEAD 當 history continuity proof。

Crash 後，任何 mutation 先遇到 transition barrier。Resolve 唯讀重讀 selection 與 journal：若 selection 指向 exact target/transition ID，可完成本機 terminal bookkeeping；若仍為 exact source，可終結為未切換，decision 必須重新檢查才可再次 apply；若兩者都不符，回報 unknown/integrity error，不猜測覆寫。Resolve 不發 Git push、不下載/上傳 secrets、不移轉 capability。重複 resolve 不改 terminal outcome。

兩端 remote HEAD 在 observation 後仍可能變動；transition 沒有跨 repo transaction。完成選擇後的每次 mutation 仍須重新驗證 destination、history、owner 與 CAS，禁止把 selection_committed 當成永久遠端信任。

## 實作驗證清單

- Source/target HEAD、owner、private dependency、caller 或 checkout 改變：apply stale，selection/refs/baselines 保持原值。
- 任一端 Git/secret unknown operation 或 active source capability：拒絕 switch；resolve/release 後重新 propose。
- Existing valid target、absent target、invalid config/Plans、non-descendant source、無法證明的 alias：分別覆蓋，不聲稱 alias identity proof。
- Crash 在 journal fsync 前後、selection rename 前後、decision terminal write 前後：restart barrier、idempotent resolve，無 push/provider write。
- Corrupt private evidence、selection、journal、linked preservation：在外部 mutation 前拒絕。
- Concurrent opposite transitions：固定鎖序與 caller serialization，不能雙重切換或取得舊 writer。
- Transition 後 source secret grant/baseline/session 保留在來源；target 不因同 project/config 或 owner session 字串而繼承授權。
- 原 checkout HEAD/index/uncommitted files 與兩端 refs 不變；公開 response 無 secret/provider raw diagnostics。

以上項目全部完成前，authority transition 保持未完成，不能用現有 namespace 分隔或 authority_binding enum 當作功能證據。

## 目前實作證據

`src/agent_handoff/authority_selection.py` 提供 caller/checkout 隔離的嚴格 selection record、journal-before-selection commit、pending barrier 與不重播的 idempotent resolve。`tests/test_authority_selection.py` 覆蓋 journal/selection 兩個中斷邊界、未知 commit point、損壞 linkage、caller 隔離與不搬移 capability/secret records。這些是本機元件測試；另以 subprocess `os._exit(77)` 驗證 commit 後磁碟復原與 kernel lock 釋放。Runtime 共用鎖已整合 selection barrier，既有 CLI mutation 路徑因而不能以不同 authority flags 繞過選擇；首次有效 observation/init 登記來源。test_authority_selection_runtime 覆蓋不同 authority 的 init/acquire/recover/release 在 observation 前拒絕，以及 pending transition 阻止 acquire/secret binding proposal。Read-only inspect 不切換 selection。雙端 consent/locks/CLI 的後續實作與證據見下段；完整 fault matrix 尚未完成。

CLI recovery：`agentctl authority resolve --checkout CHECKOUT --private-root PRIVATE_ROOT --context-file CONTEXT_FILE` 不需要 authority/ref，也不建立 Runtime/GitBackend。只輸出 transition ID、selected identity、selected/not_selected 與標準 not_attempted publication 欄位，不暴露完整 private journal。Terminal journal 的 selection 不符時回報 unknown，禁止回傳過期成功。Proposal/apply 已接入：需來源 authority/ref 與 target authority/ref，以及同 checkout/context；apply 另需 decision ID/option。

雙端 executor 目前以固定 identity 順序取得 repo locks，檢查兩端 publication/secret journals、未 revoked checkout capabilities、valid history/config/Plans、secret binding 與兩端 dependency digest。五個 transition tests 覆蓋 absent target、獨立初始化/acquisition、不 push/保留 editor/index、既有 target HEAD 前進失效、private evidence corruption/dependency stale、release 前拒絕與 CLI consent。Crash 後 decision lifecycle reconciliation 已實作；雙端 concurrent process/full fault matrix 仍待完成；不能以這些測試推導完整 transition 驗收通過。

Recovery 現在先驗證來源 binding、journal identities、完整 reviewed private/public evidence、caller lifecycle 與 linked preservation，再將 selected 終結為 applied、not_selected 終結為 stale，最後持久化 journal resolved。兩個 subprocess `os._exit(77)` 測試涵蓋 selection commit 後及 decision lifecycle write 後；重啟時 target mutation 被 pending barrier 擋住，損壞 linked evidence 不會清除 barrier，修復後不經 GitBackend 即可重複 resolve。not_selected 必須重新 propose，不重用舊同意。

結構契約：authority-selection、authority-transition、authority-decision 三份 Draft 2020-12 schemas 已加入。test_authority_schemas 驗證真實 generated records、版本/額外欄位拒絕、phase/outcome pairing、nested identities 與 absent-target owner/source nonnull HEAD。Computed identities、public/private/caller/preservation linkage 仍由 runtime 驗證；schemas 本身不證明跨 record 一致性。Wheel/sdist 中三份新增 schemas 與來源 byte-for-byte 一致。

跨 process evidence：test_authority_concurrency 使用 multiprocessing spawn，於 winner 的 durable journal 邊界保持 locks，驗證同 caller 競爭 transition 的 loser 非阻塞 rejected、winner 唯一 selection commit，以及 loser 不能從舊 source selection 重試。不同 caller 的反向 transitions 共享兩端 repo locks；競爭者 rejected，winner 完成與釋放 locks 後可安全重試。Assertions 包含 caller selection 隔離、無 capability activation、兩端 refs/source index 不變。這是 local Git 的實際獨立 process serialization 證據，不證明跨遠端 transaction 或所有 transport/fault 情境。

Target bootstrap ordering：test_existing_unbound_target_has_no_prejournal_binding_or_trust_effects 使用另一個 private root 建立真實 existing target，禁止 transition 呼叫 target.store.write，確認 switch 後 target binding/trust/capability 仍 absent。獨立 init 採納有效既有 snapshot（不 push）後才能 acquire。這個界線對 absent 與 existing unbound target 一致。
