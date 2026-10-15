# Agent Handoff Runtime
## Solution Design v1.1

- 日期：2026-10-02
- 狀態：設計提案，待原型與驗收；尚未實作。
- 取代：v1.0-draft 作為後續實作的設計基準；保留原文件供比較。
- 依據：v1.0-review 的 R01–R12，以及使用者確認的本機 `.env` 優先、Doppler 共享方向。
- 首個驗證環境：Ubuntu，包括 Windows 11 + WSL Ubuntu。

## 1. 產品與操作體驗

此 template 讓不同電腦、帳號與 coding providers 依序接續同一個開發專案。正常情況由 Agent 撰寫程式、操作 Git、維護 Plan、保存 checkpoint，以及處理環境設定。使用者以自然語言下達工作與交接要求。

| 使用者說法 | Agent 行為 |
| --- | --- |
| 繼續開發 | 檢查本機修改、恢復共享快照、取得 ownership、確認環境後繼續 |
| 今天先做到這裡 | 保存工作與交接摘要、同步已授權的 secret 變更、發布並釋放 ownership |
| 我手動改了檔案，請保留並繼續 | 比對修改、保留意圖、處理衝突後繼續 |
| 我更新了 .env，請同步設定並繼續 | 檢查差異、同步到選定 Doppler config，處理衝突後繼續 |

首次帳號建立、登入、平台授權與必要的接管決策可能需要使用者介入。正常操作不要求使用者記住 `agentctl`、Git 或 Doppler 命令。

## 2. 範圍與保證

- v1 是 sequential handoff control plane，同一受管理 checkout 同時只有一個合規 writer session。
- 保證遵守 `agentctl` 協定的遠端發布互斥；不能阻止 raw Git push、外部 editor 修改或未遵守指示的 Agent。
- 不同步聊天紀錄、不安排多 Agent 平行開發、不裁決架構與測試語意、不自動管理 PR。
- 沒有 heartbeat 或 autosave daemon；owner 的年齡不是死亡證據。
- 恢復基準是最後成功發布的 Git snapshot；另一台機器未發布的工作不保證可恢復。
- Doppler integration 是 template 的環境設定 adapter，不將 `agentctl` 擴張為通用秘密管理服務。

## 3. 架構與事實來源

| 資料 | 權威位置 |
| --- | --- |
| 可交接的程式、Plan、進度 | 權威 publication branch 的同一 commit |
| Current owner | 該 commit 的 `.agent/owner.json` |
| Protocol version | `.agent/protocol.json` |
| Active Plan、Step、blocker | `.agent/state.json` |
| 功能意圖與完成狀態 | `plans/` 中的 Plan 文件 |
| 本機執行所用 secret | 本機 `.env` 與本機 credential files |
| 跨機共享的 secret 值 | 選定 Doppler project/config |
| 上次 secret 同步基準 | Git 外的受保護本機資料 |
| Provider 登入憑證 | 各機器自己的 provider credential store |

Git 與 Doppler 是兩個独立系統，沒有跨服務 atomic transaction。已共享 code 不代表其所需 secrets 已同步；兩者必須分開回報。

## 4. Template 檔案配置

```text
AGENTS.md
CLAUDE.md
README.md
.gitignore
.env.example
.agent/
  protocol.json
  config.yaml
  state.json
  owner.json
  secrets.example.yaml
  instructions.md
plans/
  P-001-feature.md
project source files...
```

`AGENTS.md` 與 `CLAUDE.md` 指向同一份 canonical instructions，避免複製協定。Template 包含 secret 名稱與設定範例；不得包含作者的真實 secrets、登入 token 或作者 workplace 綁定。

機器專用 project/config binding、operation recovery records、secret baseline 置於 Git 外的 user-private data directory，依 repository identity 與 config identity 隔離。敏感資料使用目錄 0700、檔案 0600 或等效保護；不得放在會被一般 checkpoint 收集的路徑。

## 5. 初始化與 Ubuntu 邊界

Agent 負責確認 repository、Git、CLI、remote 權限与 runtime environment。每個 checkout 固定由一個 runtime/Git environment 操作；WSL checkout 的 Git、credentials、paths、locks 全部在 WSL 處理。

初始化必須明確選定：

1. 權威 repository identity 與 fetch/push destination。
2. Publication branch、初始 project base 與主線分支。
3. Doppler project/config、共享 key 清單與本機專用 key。
4. 本機 `.env` 位置、credential file mappings 與環境需求。

不從 `origin` 或目前 branch 猜測權威目的地；拒絕多 push URLs 或不一致的目的地。Provider binding 改變必須先確認，不能僅因 repository 檔案被修改就將秘密發往另一個 config。

建立 publication branch 使用 expected-absent conditional update；已存在就讀取驗證，不覆寫。先驗證實際 hosting backend 的 branch policy、讀取權限與 conditional update 行為。未驗證的平台/provider 不列為已支援。

## 6. 單一 Publication Branch

v1 採 review 方案 A。Owner、code、Plan、state、summary 共用同一 publication branch，避免兩條 refs 的一致性負擔。

每次 acquire、takeover、checkpoint、handoff、release 都建立新 commit，並以觀察到的遠端 HEAD OID 作為 compare-and-swap 前置條件。Control-only commit 只改 owner；它仍是完整可恢復 snapshot，但不代表新增 implementation 或重新通過測試。

最小發布契約：

1. 取得 checkout-local command lock。
2. 讀取權威 HEAD K，從 K 讀取所有協定資料。
3. 驗證 protocol、owner session、操作意圖與 snapshot。
4. 建立 C，parent 明確為 K；一般控制操作不帶入無關 local commits。
5. 驗證 ancestry，使用明確 refspec 與 `--force-with-lease=<ref>:<K>` 發布。
6. 核對結果，必要時重讀遠端；不得以普通 force push 兜底。

Expected OID 不得來自可能被背景 fetch 更新的隱含 tracking state。CAS 失敗後重新評估操作，不自動改 expected OID 重送旧意圖。

## 7. Ownership 與操作恢復

Owner 以不可重用的 session ID 識別；machine/provider 只供診斷。Generation 是可讀資訊，HEAD OID 才是發布條件。Session identity 與本機授權資料不得僅由 repository 的 narrative 推導。

| 操作 | 契約 |
| --- | --- |
| status / inspect | 唯讀，不 acquire、不更新 working tree |
| resume | 從已觀察 HEAD acquire，再 materialize 與驗證，最後才可 ready |
| checkpoint | 驗證有效 session，原子發布完整 Git snapshot |
| handoff | 同一 commit 保存工作與摘要並清空 owner |
| release | 僅自己的有效 session 可發布 control-only release |
| recover-ownership | 使用者同意綁定已觀察的 owner/HEAD；變動後舊同意失效 |
| done | 同一 commit 設 Plan DONE、cursor null、status idle；owner 是否釋放由明確操作決定 |

失去 ownership 後保留本機修改，停止受管理 publication，不將新 owner 授權自動套用到舊 session。

每次 mutation 預先保存 operation ID、expected OID、準備發布的 commit OID 與 payload identity，留在本機受保護交易紀錄。Operation metadata 亦存在 commit 中。重啟或 timeout 後核對同一操作，不能盲目新增第二筆。

| 結果 | 意義 |
| --- | --- |
| published | 能證明操作已發布；不代表目前仍持有 ownership |
| rejected | 明確遭拒絕，需重新評估 |
| publication_unknown | 無法證明成功或失敗 |
| local_only | 本機已保存，尚未共享 |
| ownership_pending | 取得或釋放權限的最終狀態尚未確認 |

## 8. Resume 與本機資料保留

Discovery 先只讀 metadata。Resume 不以先前 fetch 的 working tree 當成必然最新。

1. 檢查 dirty index、tracked/untracked changes、local-only commits、merge/rebase 狀態。
2. 保留原 checkout，必要時建立隔離的 managed checkout；不自動 reset/clean 舊工作。
3. 對觀察到的 K 建 acquisition commit K′，CAS 成功後才以 K′ 準備 execution checkout。
4. 驗證 Plan/Step/schema、本機修改整合狀態与必要環境。
5. 重新確認有效 owner；滿足可執行工作條件後回報 ready。

Materialization 失敗時以相同有效 session/revision 嘗試 release；無法確認就回報 ownership_pending。需要 secrets 的操作等待設定就緒；無 secrets 需求的檢查或測試可繼續，不宣稱完整環境已就緒。

Ignored files 不自動刪除、上傳或搬移；local preservation 與 remote checkpoint 分開。破壞性恢復前必須有可驗證 recovery handle 與保存範圍；保存失敗即停止該動作。

## 9. 使用者手動修改

每次開始/繼續工作及 checkpoint 前，Agent 檢查 diff。手動修改屬於專案工作；保留後納入當前開發，語意衝突才問使用者。

- 同一 checkout：納入檢查過的修改，不覆蓋使用者意圖。
- 另一個 checkout：保存原內容，在 managed checkout 比較與整合。
- 另一台未同步機器：無法自動發現；不宣稱已保存其內容。
- Raw push 改變 publication branch：CAS 失敗，重新驗證完整 snapshot；不直接覆蓋外部變更。
- 手改 protocol/owner 資料：驗證失敗就停止 mutation，不能视為取得權限。

發布使用已固定的 snapshot。Snapshot 建立後的新修改保留在本機，不被此次 commit 覆蓋或標示為已共享。同機不合作的 editor 可繼續寫檔；command lock 不能阻止這件事。

## 10. Plan、Step 與完成狀態

Plan 文件包含唯一、不重用的 ID、Goal、Scope、Non-goals、Steps 與 Acceptance Criteria。Plan status 僅 DRAFT、READY、DONE；目前 active Plan 由 state 指定。

| 狀態 | 不變條件 |
| --- | --- |
| idle | active_plan=null、current_step=null |
| working | active Plan 存在且為 READY；Step 若非 null 必須存在於該 Plan |
| blocked | 保留有效 active Plan/cursor，blocker 為 advisory context |
| owner released | 不代表 Plan 完成；working 仍可留待下一 session |
| done | Plan DONE、清空 cursor、status idle 一起發布 |

Step ID 不因排序或插入重編號。無 Steps 的 Plan 允許 null cursor；有 Steps 由 Agent 明確選取，不由 runtime 猜測。Plan 刪除/改名/變更須同步驗證參照；v1 保留已分配 Plan 以防 ID 重用。

Runtime 不分析 blocker 語意、不自動選下一個 Plan。Checkpoint 不代表驗收通過；測試摘要綁定受測 revision 與測試範圍，沒有證據就標示 unknown。

## 11. 本機 .env 優先與 Doppler 共享

### 11.1 模型

所有一般 token、API keys、secret configuration 以本機 `.env` 作為日常編輯與執行入口；Doppler 保存跨機共享副本。新增和修改由本機開始，並在授權範圍內同步雲端。

「本機優先」指編輯入口，不代表任何舊本機副本可以覆蓋雲端。每次比對使用三份資料：上次成功同步基準 B、本機 L、雲端 R。

無本機檔案的新機器先下載；已有 `.env` 但沒有 B 的機器必須先做初次 reconciliation：相同值建立基準、僅一側存在的 key 按來源與政策採納、同名不同值請使用者選擇。本機缺 key 不視為刪除意圖。

### 11.2 授權與初始化

- 各開源使用者建立自己的 Doppler project；同一專案的機器指向明確選定的 shared config。
- 每台 Ubuntu 安裝 CLI、首次完成登入。自動同步新增/修改的 writer 必須有更新所選 config 的權限；read-only service token 不足。
- 使用者首次啟用此模式時，一次授權 shared key 清單的新增/修改同步；日後在該範圍內不逐 key 問確認。
- 若沒有持續授權，「請同步 .env」即可授權此次同步。Production、刪除、改目的地与擴大共享範圍另需明確意圖。
- 不把真實 secrets 預置在 template，不要求將 secrets 貼到聊天。

### 11.3 Key 分類與解析

設定 manifest 區分 shared、local-only 与 file-backed 項目。只同步允許的 shared keys；機器路徑、provider 登入 token 与本機 override 不上傳。未知 key 必須先分類，不無差別 import 整份 `.env`。

使用 dotenv parser，不以 shell source/eval 執行 `.env`。v1 支援 UTF-8、明確 quoting 與字串值；duplicate keys、格式錯誤、不支援的 expansion 在任何 mutation 前拒絕。註解與排版不當作雲端資料，避免將未知項目無意刪除。

### 11.4 三方同步規則

| 自 B 起的變化 | 動作 |
| --- | --- |
| L、R 均未改 | 不處理 |
| 只有 L 新增/修改 | 上傳該 key，核對後更新基準 |
| 只有 R 新增/修改 | 保護本機檔案後更新 L |
| 兩邊改相同 key，值相同 | 接受共同值并更新基準 |
| 兩邊改不同 keys | 建立合併結果，分别發布與套用 |
| 兩邊改相同 key，值不同 | 保留原值，回報 key 名稱与衝突來源，請使用者決定 |
| L 刪除 key | 標示 pending deletion；未確認不刪雲端 |
| R 刪除 key | 保留本機，確認是否要採納刪除；不自動重建或刪除 |

基準含敏感內容或等效可比對資料，必須與 `.env` 同級保護，永不進 Git/model/logs。不將 secret value 的公開 hash 當成無敏感資訊，避免低熵值被猜測。

v1 若有任一衝突，整次 sync 不修改任一側；產生可執行的決策後重新讀取與比對。不得在對話顯示衝突秘密值。

### 11.5 同步時機與失敗

Resume、必要的 run/test 前、checkpoint/handoff 前及使用者明確要求時，由 Agent 呼叫 adapter；沒有背景監視 daemon。「自動」表示這些入口自動執行，不表示每次 editor 存檔都立即上傳。

只有有效 writer session 可上傳 shared changes。失效/離線 session 保存本機 `.env`，重新取得 ownership 后先 reconciliation，再考慮上傳。

先固定本機資料、保留本機檔案，再讀雲端并計算差異。僅送出指定 key 變更，成功核對後才更新基準；本機套用採私有暫存檔与 atomic replacement，偵測同步期間的新本機修改後停止替換。

Doppler 不假設支援 compare-and-swap 或整批 transactional update。實作前必須確認 API 的 batching、併發与錯誤契約；本機三方比對及寫入前重讀不能消除遠端 check-to-use race。v1 自動更新的保證限於所有 shared-config writers 遵守同一 ownership 協定；外部 Dashboard/API 修改與同步同時发生時，不承諾絕不覆蓋，應避免並行操作。若要求強保證，需驗證 conditional-write 能力或另行收窄操作方式。

更新 timeout/部分成功時，核對每個 intended key。未確認的項目維持 unknown/pending，不宣稱整份共享成功、不用 rollback 覆蓋別人的新值。不重新上傳舊快照；下一次讀取再 reconciliation。

網路失敗可繼續使用已有本機 `.env` 做適合的本機工作，但 secrets_shared=false。無本機必要設定就停止相關命令。已啟動程序不會自動更新環境，Agent 在適當時機重新啟動。

### 11.6 .env 與憑證檔案

Application 預設讀本機 `.env`；必要時由安全 launcher 解析并注入特定子程序。此模式不以 `doppler run` 直接取代本機編輯入口，也不將 application secrets 注入整個 coding Agent。

文字形式 PEM 可在明確 file mapping 下作獨立 shared secret，由 adapter 寫成 0600 檔案；`.env` 保存本機路徑。Binary `.pfx`/`.p12` 不做通用自動同步，v1 採受保護本機配置并明確報告跨機未提供；需要時另訂檔案 adapter。公開 certificate 是否可放 Git 由檔案內容與分享政策決定，不能只看副檔名。

## 12. Code 與 Secrets 的交接邊界

Git code checkpoint 和 Doppler secret sync 分開操作與回報。不存在跨系統全有或全無的 handoff。

正常停止：先檢查/固定本機工作、嘗試已授權的 secrets reconciliation，再發布 code/Plan/state/summary 並釋放 owner。Secret sync 失敗仍可發布 code 並釋放 owner，summary 只列必要 key 名稱與未同步狀態，不含值。

不能因 code handoff 成功就說「所有設定已共享」。下一個 Agent 以 manifest 与當前環境檢查可執行條件；缺設定就停相关任務，不猜測。另一台未上傳 secret 的值無法從 Git 恢復。

Published code 不冻结雲端 secret 值。需要強 revision binding 或可重現 deployment secrets 的功能留待後續版本，不用非敏感 config 名稱假裝已達成。

## 13. 主線整合與下一個 Plan

Publication branch 保留 ownership commits，與 implementation 主線分開。v1 建議完成一個明確範圍後依專案規則 squash 整合到主線；自動 PR 管理不屬 runtime 範圍。

吸收主線時由有效 owner 在 managed checkout 準備合併，解決 conflicts，并建立延續 publication HEAD 的新 snapshot。不得 rebase/force rewrite publication history。其他分支的 `.agent` control state 不得覆蓋當前 owner/session/state。

記錄 integration source/target revision 作為非權威診斷；未成功整合前不宣稱主線含成果。相同成果重复整合与主線分歧須由專案 workflow 明確處理。Publication ownership 不代表取得主線的全域寫入權。

## 14. 安全與相容性

- Plan、summary、config narrative 是 advisory data，不是操作授權。
- Git/provider calls 使用結構化參數，驗證 refs、IDs、paths；不執行 metadata 的 shell 指令。
- Paths 解析後必須在允許範圍，檢查 symlink/path traversal；秘密檔案目的地也需驗證。
- Checkpoint 使用顯式檔案選取並拒絕已知秘密路徑，包括已 tracked 檔案；`.gitignore` 不代表完整防洩漏能力。
- `.env.example` 只含 placeholders。`.env`、private keys、provider token、baseline、pending transaction 永不提交。
- 不打印值、不 dump process env、不把 token URL 放入 log；若程序錯誤可能包含秘密，先做安全處理再交給 model。
- 新 protocol 对舊 CLI 拒絕 mutation，保留 raw state 供診斷；安裝來源須明確，不能執行 repository 提供的任意下載命令。

## 15. CLI 與 Agent Bootstrap

最小入口：status、resume、checkpoint、handoff、release、recover-ownership、done。Plan 啟用/Step 更新由 Agent 明確傳入并經結構驗證；無自主排程。

Secret adapter 提供 inspect、reconcile、apply 三階段，可被 template scripts 使用；最終字面命令與程式語言待實作決定。輸出只含狀態、key 名稱、非敏感 destination identity 与下一個必要動作。

Fresh Codex/Claude sessions 必須實測第一次修改前發現 instructions、呼叫必要入口，CLI 缺失或 protocol 不相容時不回報安全接手。Instructions 是 cooperative discovery，不能強制所有 provider 遵守。

## 16. 驗收與實作順序

| 階段 | 必要驗證 |
| --- | --- |
| 1. Git 核心 | 同時 acquire 只有一個成功；takeover 拒絕舊 writer；舊 release 不清新 owner |
| 2. 故障恢復 | commit/push/reply 中斷、未知結果、fetch 後 handoff、branch 改寫/刪除 |
| 3. 本機資料 | dirty index、untracked binary、local commits、ignored files 與 editor 並行修改保留 |
| 4. Plan/workflow | ID/Step 完整性、done 原子轉移、主線整合与下一 Plan 基底 |
| 5. Secrets | 新機下載、初次 reconciliation、本機新增、雲端更新、不同 key 合併、同 key 衝突、刪除確認 |
| 6. Secrets 故障 | timeout、部分更新、無權限、CLI 缺失、舊 session 上傳拒絕、baseline 中斷、跨系統部分交接 |
| 7. 安全/bootstrap | tracked secrets 拒絕發布、path traversal、無值輸出、fresh provider session |

使用 WSL/Ubuntu 的 local bare Git remote 与兩個獨立 clones 建立可重複測試，再驗證選定 Git hosting backend。Doppler 用隔離測試 project/config 驗證，不使用正式 credential。外部 writer 的保證邊界亦需測試与文件說明。

Review 的 T01–T22 保留為驗收基礎；加上本節 secrets cases。原 review 只記錄局部 Git CAS 試驗；本版沒有執行 runtime 或 Doppler integration tests，不宣稱所有問題已驗證。

先完成 Git ownership/fenced publication/crash recovery 原型，再做 local-env/Doppler adapter，最後補 template UX 與 provider smoke tests。

## 17. 尚待原型確認的事項

- Hosting backend conditional update 與受保護 branch 相容性。
- Doppler 更新粒度、timeout reconciliation、併發与批次更新語意。
- Dotenv round-trip、多行值與檔案 mapping 的精確 parser contract。
- Runtime language、packaging、installer 与最終 machine-readable schema。
- 非 Ubuntu 平台、其他 providers 与大型檔案/submodule 等支援矩陣。

Safety semantics 依本版先固定；若原型與 backend 不相容，明確修訂契約，不靜默降級。

## 18. Review 對應與版本變更

| Review | 本版處理 |
| --- | --- |
| R01–R03 | 單 ref explicit CAS、session fencing、完整 snapshot、handoff 原子 release |
| R04–R05 | acquire 後 materialize、ready 再驗證、operation recovery、unknown outcomes |
| R06–R07 | 固定權威 remote/ref、主線 workflow、隔離 checkout 与保存範圍 |
| R08–R09 | Plan/Step 不變條件、Ubuntu runtime 邊界与支援矩陣 |
| R10–R12 | advisory input、secret/log boundaries、有限承諾、provider smoke tests |
| v1.0 §59 | 由 Doppler 優先改成本機 .env 起始、三方同步、明確授權与跨服務部分成功 |

References:
- Git publication: https://git-scm.com/docs/git-push
- Doppler CLI: https://docs.doppler.com/docs/cli
- Doppler projects: https://docs.doppler.com/docs/create-project
- Previous design: agent-handoff-runtime-solution-design-v1.0-draft.md
- Architecture review: agent-handoff-runtime-solution-design-v1.0-review.md

Provider 免費額度与價格屬外部政策，不寫死在 template/runtime 契約中。