# Agent Handoff Runtime — Implementation Specification

- Spec version：1.0 / protocol target：design v1.2
- 日期：2026-10-02
- 狀態：實作規格；不是 runtime implementation 或測試通過證明。
- 唯一規範來源：[Solution Design v1.2](agent-handoff-runtime-solution-design-v1.2.md)。本檔不得以 v1.0/v1.1 補足語意。
- MUST/MUST NOT/SHOULD/MAY 意義遵循 [Design §0](agent-handoff-runtime-solution-design-v1.2.md#0-規範語言與實作原則)。衝突時 design 優先；本檔的 schema/名稱是實作選擇，若需改安全契約必須同步修訂 design/protocol。

## 1. 實作基線與交付範圍

Reference：[Design §1–§2](agent-handoff-runtime-solution-design-v1.2.md#1-產品與操作體驗)、[§22](agent-handoff-runtime-solution-design-v1.2.md#22-尚待-prototype--backend-驗證的事項)。

首版選 Python 3.12+、Git subprocess、Ubuntu 24.04/WSL Ubuntu；CLI 名稱 `agentctl`，安裝以隔離 Python environment/pipx 類方式，避免變更 system Python。語言是本 spec 選擇，不是 design 強制；實作開始時鎖定相依版本。Windows native/macOS、binary certificate adapter、submodules/LFS 先標 unsupported，完成獨立驗證才擴大矩陣。

不建立 daemon、不提供 parallel orchestration、不自動建立 PR、不裁決 project semantics。Git 和 Doppler publication 是兩個獨立結果。

交付：CLI、schemas、Python package、template assets、unit/integration tests、fault injection harness、provider smoke-test scripts、README、支援矩陣與 acceptance evidence。README 的使用者流程以自然語言為主；命令參考供 Agent/維護者使用。

## 2. Package 與責任邊界

Reference：[Design §3–§4](agent-handoff-runtime-solution-design-v1.2.md#3-架構與事實來源)、[§19](agent-handoff-runtime-solution-design-v1.2.md#19-cli-surface-與-agent-bootstrap)。

```text
pyproject.toml
src/agent_handoff/
  cli.py
  contracts.py
  authority.py
  git_backend.py
  integrity.py
  sessions.py
  locks.py
  journal.py
  publication.py
  preservation.py
  materialization.py
  plans.py
  decisions.py
  orchestration.py
  paths.py
  output.py
  secrets/
    manifest.py
    dotenv.py
    baseline.py
    reconcile.py
    adapter.py
    doppler.py
    journal.py
schemas/
template/
tests/unit/
tests/integration/
tests/acceptance/
```

| 模組 | 責任 / 禁止事項 |
| --- | --- |
| authority/git_backend | Canonical destination、ref/OID read、commit construction、conditional push；不套用 project 決策 |
| integrity | Last trusted observation 與新 HEAD 比對、invalid schema、history rewrite/delete；不自動 adopt |
| sessions/locks | Capability proof、exclusive checkout binding、process-family recovery；不靠 owner.json 自我授權 |
| publication/journal | Prepared commit、durable intent、CAS、unknown resolution；不重寫歷史 |
| preservation/materialization | Recovery handle、固定 snapshot、隔離 execution checkout；不 reset/clean 原 checkout |
| plans | 結構與 references；不猜下一 Step、不判定測試合理性 |
| decisions | Control-plane envelope/revalidation；不處理 feature/architecture/test semantics |
| secrets | 敏感資料在內部解析、比對、provider IO；禁止值進一般 output |
| orchestration | Compose resume/handoff；不把兩個 backend 假裝為單一 transaction |

Backend interfaces 以 typed records 與 structured argv 呼叫，不接受 repository-provided shell snippet。所有 mutation 共用 transaction coordinator，不在各 command 重複實作 fencing。

## 3. Authority 與資料存放

Reference：[Design §3.1](agent-handoff-runtime-solution-design-v1.2.md#31-repository-identity)、[§4](agent-handoff-runtime-solution-design-v1.2.md#4-template-與本機私有資料配置)、[§5.1](agent-handoff-runtime-solution-design-v1.2.md#51-不得猜測-authority)。

Repository config 保存經核准的 non-secret authority description；Git 外 bindings 記錄已核准 identity。Repository config 被改不自動換目的地。

- `repository_identity`：由 canonical hosting identity、repository immutable ID（可取得時）、canonical destination、publication ref 的版本化 encoding 產生 stable digest。不是 authentication token。
- URL 移除/拒絕 embedded credentials；不把 credential URL 用於 logs/digest input。
- SSH/HTTPS alias equivalence 需 backend identity proof，不以字串猜等價；無法證明要求 authority-binding decision。
- 初次 bootstrap 使用 explicitly supplied authority，分辨 expected-absent init 與已註冊 authority 遺失。後者不得自動 recreate。
- V1 只允許單一確認過的 fetch/push repository destination；多 destination fail closed。

Private root：`${XDG_STATE_HOME:-~/.local/state}/agent-handoff/`，取得後 realpath 驗證不在 repository/其 nested roots，owner 為目前 uid，root/children 0700、敏感檔案0600。對 JSON writes 使用同目錄 temp、fsync(file)、rename、fsync(parent)。拒絕 symlink、錯 owner 或不安全 permissions，不默默接管其他人的 private state。

Private layout 延續 Design §4.2，另含 `integrity.json`、`checkout-binding.json`、secret journals 與 decision-private evidence。這些不是共享權威來源，不能凌駕 remote ref。

## 4. Records / Schemas

Reference：[Design §8](agent-handoff-runtime-solution-design-v1.2.md#8-ownership-與-session-model)、[§10](agent-handoff-runtime-solution-design-v1.2.md#10-operation-journal-與-crash-recovery)、[§11.3](agent-handoff-runtime-solution-design-v1.2.md#113-decision-envelope)、[§14](agent-handoff-runtime-solution-design-v1.2.md#14-planstep-與完成狀態)。

所有 records 帶 `schema_version`；MUST 拒絕未知 incompatible version、invalid required fields、duplicate JSON keys。一般 records 禁止 extra properties；允許明確 `extensions` namespace，不能改授權語意。OID 驗證依 repository object format，不能假設永遠 SHA-1。

| Record | 最小欄位與規則 |
| --- | --- |
| protocol.json | schema_version、protocol_version、required_features；不相容時只允許安全 readonly diagnostics |
| config.yaml | authority description、publication_ref、mainline_ref、plan_directory；無 credentials 或 adopter-specific Doppler binding |
| owner.json | schema_version、owner:null 或 {session_id,acquisition_operation_id,machine_label,provider_label}；labels 僅診斷 |
| state.json | schema_version、status:idle/working/blocked、active_plan:null/string、current_step:null/string、blocker:null/advisory record、summary；不存自身 commit OID |
| commit metadata | operation_id、operation_type、session_id/null、payload_identity；存於受保留的 commit trailers，禁止 secrets |
| local session | repository_identity、publication_ref、session_id、acquisition_operation_id、acquisition_oid、checkout_identity、provider/runtime labels、schema/protocol version、revoked |
| Git journal | operation_id/type、repository_identity/ref、expected_oid、prepared_commit_oid、payload_identity、session_id、created_at、phase、result |
| decision envelope | decision_id/category、repository_identity/ref、observed_head/owner、destination_identity/null、reason、options、invalidated_by |
| preservation handle | handle_id、original_checkout、HEAD、index evidence、scope、method、verification evidence；敏感 paths 留 private |
| integrity observation | repository_identity/ref、last_trusted_oid、current observation、reason、pending decision；時間僅診斷 |

Session/operation/decision IDs 使用 OS CSPRNG 至少128 bits，不重用。公開 session ID 不是秘密；真正 proof 是 Git 外 record 與本機 lock/binding。不能只拿 ID 自稱 owner。

Payload identity 只針對 non-secret Git snapshot/control intent；secret payload fingerprints 不得放進 Git/公共 envelope。Schema files 必須有 valid/invalid fixture tests。

## 5. CLI 與 Machine-Readable Result

Reference：[Design §9.1](agent-handoff-runtime-solution-design-v1.2.md#91-vocabulary)、[§19.1–§19.2](agent-handoff-runtime-solution-design-v1.2.md#191-最小邏輯入口)。

| Command | 輸入 / 行為 |
| --- | --- |
| init | explicit authority/ref/base；existing branch validate；不是 runtime 隨機猜 remote |
| status / inspect | revision-pinned readonly report；不 acquire/materialize/update worktree |
| resume | current caller/private context；orchestrate discovery→preserve→acquire/recover→materialize→validate |
| acquire | owner null；新 session；CAS commit |
| recover-session | private capability valid；恢復同 session，通常不發布 commit |
| takeover | valid decision_id/option；新 session；不是 recover-session 別名 |
| checkpoint | explicit include-list、state changes、summary；owner 保留 |
| handoff | explicit include-list、summary；完整 snapshot 和 owner=null 在同一 commit |
| release | control-only；不改 Plan completion |
| done | active Plan completion；release intention MUST 明確 true/false，未提供則拒絕 |
| operation resolve | operation_id；核對未知結果，不重發新意圖 |
| decision apply | decision_id/option_id；re-read、stale check、執行核准 intent |
| secrets inspect/reconcile/apply/verify | non-secret output；provider values 僅在 private adapter boundary |

禁止 `recover-ownership`，回報已移除並指出正確語意，不偷偷映射 takeover。

Common result：

```json
{
  "schema_version": 1,
  "command": "handoff",
  "status": "completed",
  "operation_id": "opaque-id",
  "published_revision": "oid-or-null",
  "code_publication": "published",
  "ownership": "released",
  "secrets_shared": false,
  "decision": null,
  "next_action": null
}
```

`status`：completed/ready/not_ready/decision_required/rejected/unknown/invalid/unsupported。`code_publication`：published/rejected/publication_unknown/local_only/not_attempted。`ownership`：owned/released/fenced/ownership_pending/unowned/unknown。`secrets_shared`：true/false/partial/unknown/null（未配置或不需秘密）。所有 enum 定義在 schema，不能混入 project blocker。

Exit codes：0 完成（包含明確回報的 partial handoff）；2 input/schema；3 decision required；4 conflict/fenced/CAS rejected；5 unknown；6 setup/auth/network unavailable；7 integrity violation；8 unsupported。Exit 0 不代表 secrets 全成功，consumer 必須讀分離欄位。stdout 僅 JSON，stderr 使用安全診斷，default traceback 不可輸出敏感 argv/response。

## 6. Locks 與 Session Recovery

Reference：[Design §6.1](agent-handoff-runtime-solution-design-v1.2.md#61-最小-publication-contract)、[§8.3–§8.5](agent-handoff-runtime-solution-design-v1.2.md#83-local-session-capability-record)、[§9.3](agent-handoff-runtime-solution-design-v1.2.md#93-recover-session)。

Linux 使用 kernel-backed exclusive lock（例如 flock），不要以 timestamp 判定 stale。取得顺序固定：repository mutation lock → session lock（若已知）→ checkout command lock。所有 calls 順序一致；取得失敗即拒絕 mutation。

另存 exclusive write-capable checkout binding，跨 CLI invocation 持續有效。短期 command lock 只防兩個命令同時執行，不能代替「同 session 只綁一個 write checkout」。不同 process family 不自動共用 capability；新 checkout 要經明確保留/移轉流程，不能覆寫 binding。

Acquire/takeover before-send journal 保存候選 session identity；remote success 但 session record 尚未寫入時，operation resolve 可用該 private journal 重建 record。Recover-session 檢查 Design §8.4 全部六條，pending journal/integrity ambiguity 必須先 resolve。Process restart 不視為 new takeover，也不從 machine label 找回 session。

## 7. Publication Transaction

Reference：[Design §6](agent-handoff-runtime-solution-design-v1.2.md#6-single-publication-branch-與-cas-publication)、[§9](agent-handoff-runtime-solution-design-v1.2.md#9-ownership-operations-與-state-machine)。

共用演算法：

1. 驗證 inputs、authority binding、private permissions；取得 locks。
2. 直接查 remote ref 得 K；fetch exact authority snapshot，若讀取間 ref 前進，重新觀察，不能拼不同 revisions 的 owner/state。
3. 比對 integrity、讀 K 的 protocol/owner/state/Plans，驗證 session與 intent。新 authority init 為獨立 expected-absent path。
4. 保留/固定本機工作，explicit selection，secret path deny-list。
5. 使用獨立 temporary index/Git plumbing 建 tree，避免污染使用者 index。C 只有 parent K；不偷帶其他 local commits。
6. Commit trailers 写 operation metadata，驗證 tree/schema、parent(C)==K；prepare journal durable 後才允許 network write。
7. Conditional push exact C→authority ref、explicit expected K。不得普通 force 或刪 ref 重建。
8. Record response並直接核對 authority。Resolve publication evidence；再 activation/revocation session capability。
9. 回傳結果並保留本機 snapshot 後的新修改。所有 journal transitions durable。

Checkpoint 保留 owner；handoff 同 commit 清 owner；release 只改 owner/control metadata；done 原子更新 Plan DONE、active_plan/current_step null、idle，release 按 explicit intent。

CAS reject：讀新 HEAD、驗 integrity、原 operation terminal rejected；正常 descendant 可建立新 operation，但需重新評估 owner、decision、snapshot，不能換 K 重送舊 intent。

## 8. Crash、Unknown 與 Integrity

Reference：[Design §7](agent-handoff-runtime-solution-design-v1.2.md#7-publication-integrity-violation)、[§10](agent-handoff-runtime-solution-design-v1.2.md#10-operation-journal-與-crash-recovery)。

Journal phases：prepared → sending → verifying → resolved。Sending 前 fsync；crash 在任何階段都能從 prepared OID 恢復。Local objects 由 private/local recovery ref pin，防 GC 删除待判定 commit；這不是遠端 control ref。

Resolution：

- remote HEAD==C，或可信 authority history 包含 C 且 operation metadata 一致 → published。
- published 不代表仍是 owner；另外核對最新 session。
- remote K 且 backend 可證明無 pending write → 可 retry 原 C，同 operation_id。單次 ref observation 不足以證明 timeout 的 server command 已結束；不能直接重試別的 owner intent。
- history 無法完整取得、rewrite/delete、無法證明結果 → unknown/integrity，停止依賴該結果的 mutation。
- 不以找不到 operation 當作未發布證據，不重建等價 owner commit。

Integrity validator 檢查 last trusted K→new HEAD descendant、protocol/owner schema、authority identity、ref deletion。Readonly 可以報 observed invalid raw facts，但不更新 trusted baseline、不自動 adopt。

提供 adopt/abort 等實際可執行 decision。Adopt 綁 exact observed HEAD、preservation evidence、schema validation；不能採納 invalid snapshot。Restore/rewrite authority 是危險 recovery exception：首版未完成專用 backend/backup/conditional-restore proof 前標 unsupported、不顯示該 option；不得用一般 publication pipeline 執行 force restore。此 gate 落實 Design §7.2，不偷偷放寬 §6 的禁 force 規則。

## 9. Preservation、Snapshot 與 Resume

Reference：[Design §12–§13](agent-handoff-runtime-solution-design-v1.2.md#12-resumematerialization-與本機資料保留)。

默認保存方法為保留原 checkout、使用 private managed checkout；不得移動/刪除原 ignored files。Preservation report 分開列 tracked、index、untracked、ignored、local commits 与 merge/rebase。若將採 destructive action，備份需經 byte/index/commit restore validation，否則停止。

Fixed snapshot 使用 explicit included paths 取得 blob records，保留 binary/symlink file mode；不無差別 git add。Editor 並行寫入可能造成不一致：逐檔 pre/post stat + content reads 與 final collection verification，偵測變化就重新固定或停止。這不是 OS-level atomic multi-file snapshot；不宣稱 external editor 被 lock 阻止。

Resume 流程依 Design §12.1：inspect→preserve→owner routing→acquire/recover或decision→materialize exact committed revision→validate Plan/Step/local integration/env→re-read owner→ready。新 acquisition 後 materialization fail 才嘗試有效 session release；unknown 回 ownership_pending。

Materialize 只在 disposable clean managed checkout使用經驗證的 Git操作；禁止 reset/clean 原 checkout。User-local edits由 Coding Agent 比較与整合，runtime只提供安全資料，不裁決語意。

Readonly不改 worktree/index/owner/state；可在 private namespace cache fetched objects，不污染一般 tracking refs或 trusted observation。Environment secret 缺失僅阻擋依賴它的工作，ready output列明 availability，而不假稱全部設定就緒。

## 10. Plan 與 Mainline

Reference：[Design §14](agent-handoff-runtime-solution-design-v1.2.md#14-planstep-與完成狀態)、[§17](agent-handoff-runtime-solution-design-v1.2.md#17-主線整合與下一個-plan)。

Plan採 Markdown +明確 metadata block，包含 immutable id、status和 stable step IDs；schema/Markdown parser不透過模糊檔名猜 ID。所有 checkpoint 對完整 Plan set 驗 duplicate/missing references。禁止刪除已分配 Plan 或重用 ID；可改名但同步 references。

`idle` 必須 null active/cursor；working active READY且有效；blocked 保留有效 active/cursor。Step選擇由Agent傳入，無Steps可null。Owner release不改working。Test evidence需 tested_revision/scope/status；無有效證據passed不得呈現，control-only commit不等於再測。

主線整合由 coding workflow決定merge/squash/cherry-pick；runtime輸入經核准 snapshot，維持publication单parent K。Incoming mainline tree不得覆寫control files。Diagnostic integration revisions不作authority。無PR lifecycle或mainline全域lock保證。

## 11. Decision Engine

Reference：[Design §11](agent-handoff-runtime-solution-design-v1.2.md#11-decision-only-human-intervention-contract)、[§15.6](agent-handoff-runtime-solution-design-v1.2.md#156-secret-decisions)。

Envelope immutable，狀態pending/applied/stale/aborted置於獨立private lifecycle record。Options使用allowlisted executor type与參數，不能讓repo narrative提供command。Category限定control-plane。

Decision apply重新讀HEAD/owner/destination及relevant secret state；任何design列出的變化即stale，不套舊同意。Only safe viable options after preservation，Natural-language reason/effect由Agent呈現；使用者不需操作Git/CLI。

Secret decision envelope禁止values及公開value hashes；private evidence保存B/L/R comparison material或private keyed fingerprint，same protection asbaseline。UI只有key names、來源、選項。Scope/production/destination intent亦綁觀察與授權，不從manifest變更推斷授權。

Project test bug、需求、code style等絕不进入engine；提供typed rejection以防混用，交Coding Agent一般workflow。

## 12. Secrets Manifest、Parser 與 Baseline

Reference：[Design §15.1–§15.8](agent-handoff-runtime-solution-design-v1.2.md#15-本機-env-優先與-doppler-共享)。

Template `secrets.example.yaml`提供非敏感宣告：schema_version、key name、classification:shared/local-only/file-backed、required、non-secret file mapping。實際approved Doppler binding及 persistent grant在Git外，manifest擴大scope必須新decision。

本機L為日常執行/編輯入口，R為共享副本，B為last verified convergence。只列入shared allowlist；unknown key不上传；provider token、machine path和local override排除。Local-only不進B/R。

Parser contract選擇：UTF-8、LF/CRLF、KEY regex `[A-Za-z_][A-Za-z0-9_]*`、空字串vs缺失分別表示、單/雙引號和受測multiline quoting；duplicate key或不支援expansion拒絕。不得shell source/eval。保留raw spans用于只更新指定key，不丟註解、local-only項；無法可靠round-trip則停止apply，不能重寫整份檔案。Escape rules需fixtures冻结，依Design §22 prototype gate。

Baseline private record：schema_version、repository/config identity、shared scope、per-key present/value、verified sync ID；敏感serialize不經一般logger。Separate fromnon-secret result。Local-env mode必須明確選擇，不因Dopplerfail偷偷fallback；未配置secret時readonly和無secrettest可用。

## 13. Reconciliation 與 Apply

Reference：[Design §15.5–§15.12](agent-handoff-runtime-solution-design-v1.2.md#155-三方同步規則)。

Pure function `reconcile(B,L,R,scope,grant)`輸出internal mutation plan，不做IO。

| 條件 | 計畫 |
| --- | --- |
| 新機無L有R | 建L及B，仅approved sharedkeys |
| 有L/R無B相同 | 建B |
| 有L/R無B同key不同 | decision，无mutation |
| 無B只一側存在 | 依scope/授權新增或下載，不把缺失視為刪除 |
| 只有L新增/修改 | upload changedkeys，verify後B |
| 只有R新增/修改 | preserve L、套R，verify後B |
| 雙方變成相同值 | 接受並更新B |
| 改不同keys | merge，apply並verify |
| 同key不同值 | conflict，整次停止 |
| L/R任何一側刪除 | deletion/adoption decision；未決不刪、不自動重建 |

任何conflict/deletion待決使整次reconciliation不mutation，不實作獨立key transaction例外。

Apply重新讀L/B/R、核對plan及decision不stale、repo/session locks與owner、destination/grant有效。秘值只在private adapter記憶體与必要fileIO，never output/model。

Steps：durable private secret intent→必要Doppler per-key updates→verify每key→safe local patch→verify local→atomic baseline update。Partial commit不能把整份B改成假成功；維持verified baseline與pending per-key evidence，下一次先resolve再reconcile。沒有rollback覆蓋others、沒有bulk import舊L。

Local file replacement使用same-directory0600temp/fsync/rename；source changed就停止，保留temp/privateevidence。External editor最後check與rename仍有競態，不能保證任意不合作editor完全無lost update；正常apply期間需Agent/user避免編輯，測試注入已可觀察變更，不能宣稱general filesystemCAS。

Offline可用L做適合本機工作，secrets_shared=false；禁止offlineupload。每次送出前檢查Git ownership，已失效session拒絕上傳；這不是跨Git／Doppler的provider-side fencing。

PEM file-backed shared值經approvedmapping寫0600file；`.env`保存localpath而不上傳該path。Binarypfx/p12提供unsupported/not_provided結果，不塞進dotenv。

## 14. Doppler Backend Capability Gates

Reference：[Design §15.9–§15.10](agent-handoff-runtime-solution-design-v1.2.md#159-writer-fencing)、[§22](agent-handoff-runtime-solution-design-v1.2.md#22-尚待-prototype--backend-驗證的事項)。

Interfaces：read scoped values、set specified key、delete approved key、verify intended values、inspect auth/capabilities。CLI/API調用應將敏感input放stdin/private file，不放argv；response capture隔離不輸出。

上線自動upload前需實測：批次更新是否partial、timeout延遲寫入、permissions、delete behavior、limits、secret comparison與binding。Backend不支援atomicupdate則採per-keyjournal而不假裝transaction。

**使用者確認的基本同步契約**：依Design §15.10的基本版修訂，Doppler shared upload使用同步單key寫入、寫前讀值比對及寫後驗證，不要求provider lease、atomic CAS或terminal-status API。仍需有效Git ownership、已批准的destination／scope與upload grant。成功才更新baseline；明確拒絕保留baseline；逾時或不明結果保留unknown並等待`resolve`觀察目前值，且不重送。

`resolve`在可讀到目前遠端值時確認目前的收斂／差異；收斂才推進baseline，否則保留baseline、結束這次確認並要求重新reconcile。它不證明歷史request的server terminal outcome。這個cooperative契約是使用者明確選擇的基本版範圍，不提供跨服務transaction或同key原子競爭保證。

另一gate：DopplerDashboard/API外部writer與read/verify間仍可競爭。偵測变更即停止並reconcile，未經backendproof不承諾完全防overwrite。文件清楚區分detection與prevention。

## 15. Handoff Orchestration 與安全

Reference：[Design §16](agent-handoff-runtime-solution-design-v1.2.md#16-code-與-secrets-的-handoff-boundary)、[§18](agent-handoff-runtime-solution-design-v1.2.md#18-安全與相容性)、[§5.2](agent-handoff-runtime-solution-design-v1.2.md#52-host-preservation-guardrail)。

Freeze local Git snapshot→嘗試已授權secretreconcile→Git CAS handoff；secretsfail不阻止可安全codehandoff，但結果必須分離。Summary只含requiredkeynames與syncstatus，无credentialvalues。Publishedcode不freezeR，不提供reproduciblesecretversion承諾。

Safety：realpathwithinallowlist、symlinkescape拒絕、Gitcheck-ref-format等验证、structuredargv、只allowlisted secret destinations、denytrackedcredentialpaths。不是任意source中的secret scanner保证。

Host setupinspect现有工具/versions/config，minimumchange、可驗證backup，禁止預設停VPN/DNS/firewall/SSH/globalGit/provider設定。Installer無權修復時回setup_error及可由Agent执行的方案；外部OAuth/2FA才交人類。不得安裝任意repo下載腳本。

Log/redactiontests使用sentinel secrets覆蓋stdout/stderr、tracebacks、journal-visibleerrors、Gittrailers、summary与model-facing output。Hash/tokenURL/私有envelope同級保護。Genericunexpectedexceptions也需safeerror，不輸出raw provider response。

## 16. Acceptance Plan 與 Traceability

Reference：[Design §20](agent-handoff-runtime-solution-design-v1.2.md#20-acceptance-contractself-contained)、[§21](agent-handoff-runtime-solution-design-v1.2.md#21-實作順序)、[§25](agent-handoff-runtime-solution-design-v1.2.md#25-implementation-definition-of-done)。

A01–A63 的完整情境與expectedresult以Design §20表格為唯一baseline；test case / test evidence必須携带原ID，不重新命名稀釋要求。以下為實作分配，**不是測試已通過**。

| Test suite | Design IDs | 測試方法 |
| --- | --- | --- |
| ownership_cas | A01–A10 | bare remote+兩clones，barrier固定race，檢查remote owner與OID |
| snapshot_handoff | A11–A15 | commit tree assertions，done intent，editor改動fixture |
| crash_recovery | A16–A20 | push前/後/回覆丟失kill，重啟journalresolve，驗不重複 |
| integrity | A21–A24 | delete/non-descendant/invalid metadata+stale decision |
| preservation | A25–A30 | index/content/binary/localcommit/ignored bytes，雙checkoutprocess |
| decision_boundary | A31–A35 | decisionUXrecord/staleness；projectsemantic不路由engine |
| plans | A36–A40 | invalidreferences/stableIDs/workingrelease/testevidence |
| secrets | A41–A54 | deterministic fakebackend + isolatedliveconfig，B/L/Rmatrix，逐keytimeout |
| security_host_bootstrap | A55–A60 | trackedsecrets、symlinks、hostfixtures、fresh provider session、versionmismatch |
| partial_handoff | A61–A63 | 每種Git/Dopplerpartialpair，requiredsecret缺失 |

補充tests：init race/expected-absent、credential URL redaction、filefsync/renamecrash、same-session 持續 checkout binding、Doppler owner precheck 後的 takeover race、multiline / dotenv round-trip、local replacement race。這些補充不替代A01–A63。

Fault injection points：after preservation、journal fsync、before remote send、after server accept、beforeclientreply、before capabilitywrite、beforematerialization、before/aftersecretkeywrite、beforeverify、beforelocalrename、beforebaselinewrite。測試以barrier/fakeclock不靠不穩定sleep猜時序。

每個evidence record包含ID、designreference、fixture/backend version、command/test version、expected/actual、pass/fail/unsupported、sanitized artifacts。Mock只能證明local邏輯；至少真實hostingbackend與isolated Doppler需驗證backendbehaviour。

## 17. 分階段交付與完成門檻

Reference：[Design §21.1](agent-handoff-runtime-solution-design-v1.2.md#211-測試環境)、[§25](agent-handoff-runtime-solution-design-v1.2.md#25-implementation-definition-of-done)。

1. Schemas/authority/localprivateinfrastructure、Gitcore、最小decisionvalidation：A01–A10/A29。Takeover不能先做不驗decision的暫時捷徑。
2. Journal/integrity/preservation：A16–A30；未知結果不得先用重試掩蓋。
3. Plan/checkpoint/handoff/done/mainlineguards：A11–A15/A36–A40。
4. 完整decisionengine/AgentUX：A31–A35；shared核心自階段1已遵守stalevalidation。
5. Parser/baseline/local-env與Dopplerprototypegates，B/L/R：A41–A54/A61–A63；gate未通只交付local-env / inspect，不標全功能verified。
6. Host/security/provider/bootstrap/livebackend：A55–A60，全部acceptance evidence。

Definition of Done照Design §25十項，A01–A63全部可重複evidence、至少真實Gitbackend、Ubuntu/WSL、fresh provider、isolated Doppler、leak/stale/hosttests。Unsupported清楚标示，但若它是完整v1.2必要功能则不得以unsupported当作pass。

## 18. 待決策與非降級原則

| Item | 決策 / Gate | Design reference |
| --- | --- | --- |
| Doppler strong writer fencing | owner check 到 provider write的race需backendproof；未解不開自動upload | §15.9、§22 |
| Integrity restore | 首版不提供，直到special recoveryproof與design一致 | §7.2、§6.1 |
| Dotenv parser round-trip | 明確fixtures与syntaxfreeze；不以shell解析 | §15.4、§22 |
| Hosting identity / CAS | 每backend實測，no implicit origin / force fallback | §3.1、§5.1、§6 |
| Arbitrary external editor writes | 固定snapshot/變更偵測；無OS atomic multi-file 保證 | §13 |
| Provider bootstrap | 每provider/environment smoke test後列supported | §19.3 |

本規格已完整對應v1.2；它使未封閉的backend能力問題可見，不能把proposal、mockpass或CLI能啟動當作verified runtime。
