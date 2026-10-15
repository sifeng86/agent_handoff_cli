# Agent Handoff Runtime — Architecture Review

- Initial review: 2026-09-28
- Deep review / revision: 2026-09-30
- Reviewed version: `1.0-draft`，共 58 節、2,167 行。
- Source: [agent-handoff-runtime-solution-design-v1.0-draft.md](agent-handoff-runtime-solution-design-v1.0-draft.md)
- Review type: 文件架構審查；工作目錄只有設計文件，沒有 runtime 實作或其測試；另做單一 Git CAS 機制試驗（§7）。
- Decision: **產品範圍可保留；核心一致性契約尚未具備實作凍結條件。**
- 本檔是獨立 review 與修訂提案，不代表原設計已採納或完成驗證。

## 1. 整體判斷

將產品限制在 sequential handoff control plane 是合理方向。Git 保存可轉移狀態、Plan 保存意圖、Agent 處理專案語意，以及不引入 daemon、Plan Registry、對話同步或自動排程，均符合 v1 規模。

主要缺口不是功能不足，而是「何時算取得 ownership、何時算 checkpoint 成功、何時能讓下一個 Agent 安全接手」尚未形成可驗證的交易契約。§49 已列出部分解法，但幾項只是目標敘述，還不能視為已解決。

此次深查修正 Astra review 的三個判斷：雙 ref atomic push 改為條件式備選；§19/§21 的順序問題改列規格歧義；WSL 改為首個驗證環境，不預設為全產品唯一平台。建議維持產品範圍，先確立三個架構決策：

1. 保證範圍是遵守協定的 `agentctl` publication；一般 Git credentials 不是可撤銷的 Agent session 權限。
2. ownership fencing 與 checkpoint publication 必須共用可證明的遠端序列化邊界；單一 ref CAS 是 v1 優先候選。
3. resume 使用明確的 published revision，且取得 ownership 後才可宣稱 ready。

P1 表示實作前必須解決，否則可能接受失效 writer、遺失工作或錯誤恢復；P2 表示 v1 契約與驗收需要補齊。R01–R11 延續 Astra review 的編號，R12 是此次深查新增。以下為設計缺口，不是已發生的 production bugs。

## 2. Findings

### R01 · P1 — Ownership revalidation 無法單獨阻止舊 writer 發布

**位置：** §17、§49.24、§50 I1–I3；L703–738、L1928–1954。

**失敗時序：** A 檢查 owner generation 7 後暫停；B takeover 成 generation 8；若 A 的 publication 不以遠端 generation 變化作條件，A 仍可能推送。§49.24 的「push 前重新驗證」存在 check-to-use race。尤其兩條獨立 refs 時，若 B 只更動 ownership ref，A 的 source push 仍可能是 fast-forward。

**深度修訂：** Astra 的「兩個 refs + atomic push」是可行候選，但不是 v1 唯一解。**建議先驗證單一 publication branch 的 CAS**：owner record 與 code/state/Plan 同屬該 branch 的 commit snapshot；acquire、takeover、checkpoint、handoff、release 每次都建立上一個 remote HEAD 的 descendant commit，並以該 HEAD 的明確 OID 作 publication 前置條件。B takeover 會推進同一 ref，A 帶舊 expected OID 的 push 必須失敗。owner session ID 不可重用；release 也只在自己的 session/revision 仍有效時發布。

`--force-with-lease=<ref>:<expected>` 會比對指定 remote ref；它同時可能允許 non-fast-forward 更新，所以 runtime **另須驗證新 commit 是 expected HEAD 的 descendant**，且不得用 `--force` 或 `+` refspec 兜底。[Git push documentation](https://git-scm.com/docs/git-push)

**保證範圍：** 此機制只約束 compliant `agentctl` clients。帶 credentials 的 raw Git push、改寫遠端分支、使用者手動修改檔案都無法靠 client-side lease 禁止，詳 R02。

**驗收：** A 在最後一次 local check 後暫停；B takeover；A 的舊 expected OID publication 必須失敗。另測 B release/reacquire、同機雙 session、非 fast-forward 與 raw Git bypass 的保證邊界。

### R02 · P1 — One writer 的保證範圍與執行能力不一致

**位置：** §1、§4.3、§49.18、§50 I1；L19、L169–173、L1880–1884、L1942–1944。

前文要求一次只有一個 Agent 修改專案，後文縮小為 handoff-managed writes。遠端 ownership 無法停止失聯機器的 editor、本機檔案修改，或持有 Git credentials 的 raw push。同機兩個 session 也不能因 machine identity 相同就共用 ownership。

**修訂建議：**

- v1 採 cooperative protocol：保證 compliant clients 的共享發布互斥；失效 session 的本機修改只能保留為待恢復工作。
- owner identity 使用每個 session 唯一 ID；machine/provider 只供診斷。
- 若要阻擋繞過 CLI 的 raw push，需要 server-side policy 或獨立授權服務；bootstrap instructions 本身無法強制執行。
- 每個 execution checkout 只允許一個 writer session；CLI mutation 另加本機 command lock，避免同一 owner 同時執行兩個 checkpoint。此 lock 不代表能攔截 editor 寫檔。
- 失去 ownership 後停止受管理寫入、保留 local changes；不能把新 owner token 自動套用到舊 session。

**驗收：** 同機第二 session 無法冒用第一個 session；保證明確區分 remote publication fencing 與 local edit isolation。

### R03 · P1 — Checkpoint 與 handoff metadata 尚未定義 publication 邊界

**位置：** §15、§19、§21、§45、§49.16；L636–646、L788–798、L869–876、L1554–1562、L1864–1868。

§19 與 §21 的步驟順序是**規格歧義**，不能直接斷言既有實作必然會出現 mixed snapshot。但若分兩次 push 或 state/code 分開提交，crash 就可能留下新 code 配舊 cursor。`state.json.last_checkpoint` 若想指向包含自己的 commit，也有 self-reference 問題。

**修訂建議：** 單一 publication branch 的每個 HEAD 都是完整 snapshot。checkpoint commit C 一次包含 source、Plan status、cursor、summary 及 owner record；handoff commit H 可同時保存 WIP 與清空 owner；done 同一 commit 完成 Plan → DONE、cursor → null、status → idle。control-only acquire/release/takeover commit 可以維持相同 code/state tree，只更新獨立的 `.agent/owner.json`；因此 ownership 仍與 `state.json` 分開，但遠端以同一 ref CAS 序列化。

移除 persisted `last_checkpoint` 自我 OID 欄位；CLI 回傳的 `published_revision` 取自實際 remote HEAD。control-only commit 也是可恢復的 shared snapshot，不能讓使用者誤以為它代表新 code 或通過測試。若產品一定要單獨顯示最近一次「code checkpoint」，可從提交 metadata 導出，不另存易衝突的 canonical pointer。

**驗收：** 任一 commit/push/reply 中斷後，incoming Agent 從一個已發布 HEAD 讀取完整 snapshot。DONE 與 cursor 不出現跨 commit 的半完成組合；`last_checkpoint` 不回退一版，也不引用不存在的自身 OID。

### R04 · P1 — Resume 可能帶入過期 snapshot，且 acquisition 成功不等於 ready

**位置：** §3、§23、§25、§42；L130–141、L959–970、L1024–1037、L1500–1507。

§23 的步驟列先 sync、後 acquire、再 inspect checkpoint，尚未明確規定後者一定重新讀 remote，因此此處是**未封閉競態**：B 先 fetch C1；A 發布 C2/release；B acquire 後若沿用 C1 working tree，就錯過 C2。

**修訂建議：** discovery 只讀取 metadata，不改現有 working tree。B 針對觀察到的 remote HEAD K 和 owner state 建立 acquisition commit K′，以明確 expected K 做 CAS。若 A 在其間發布 C2，B 的 CAS 失敗，必須重新讀取，不能以舊意圖自動接手。成功後，B 取得 K′，確認其 parent 為預期 K、owner 為自己，保護 local changes，將 K′ materialize 到受管理 checkout，再驗證 Plan/Step/schema，最後才回傳 `ready`。

如果本機已有 dirty worktree、rebase/merge 中、local-only commits，先建立可驗證的保存方式或使用隔離 checkout；不能把 fast-forward 當成通用恢復動作。materialization 失敗時，owner 已可能在遠端成立；以同一 session/revision CAS release，release 結果不明時回報 `ownership_pending`，不得宣稱未持鎖。

**驗收：** B fetch 後 A 才 handoff；B 的舊 CAS 必須失敗並重新觀察 C2。dirty worktree 不被覆蓋，acquisition 後 materialization 失敗不回傳 ready。

### R05 · P1 — 遠端操作 timeout 缺少結果未知與冪等重試

**位置：** §21–23、§46–47；L858–970、L1568–1602。

server 已完成 push，但成功回覆遺失時，client 不知道結果。直接重做 checkpoint、takeover 或 release 可能產生重複 commit，或干擾新 owner。

**修訂建議：** 每次 mutation 使用穩定 `operation_id`，在所發布 commit 的 metadata 記錄；同一重試保留相同 ID 與 payload。先重新讀權威 ref：若該 OID 就是預先計算的 commit，視為成功；若已被後繼 commit 推進，可在受控歷史中查找該 commit/ID；若無法證明已提交或未提交，回報 `publication_unknown`。明確 CAS 失敗後重新取得最新狀態、重新評估意圖，不自動換 expected OID 重送舊要求。

不要求額外 model journal 或永久 operation registry。歷史可能遭協定外改寫或被 hosting policy 隱藏，因此查不到 operation 不代表未提交。設定有限重試，斷網時不得宣稱「跨機保存完成」。

**驗收：** 丟棄 acquire、checkpoint、handoff、release 的成功回覆；相同 operation 不能重複發布或移除後繼 owner。區分 `published`、`rejected`、`publication_unknown`、`local_only`。

### R06 · P1 — 權威 remote / branch 與既有 Git workflow 未定義

**位置：** §17、§19、§21、§25、§42；L711–723、L778–798、L880–915、L1016–1055、L1502–1507。

shared Git remote 未指定權威 repository/ref。不同 clone 的 `origin` 可能指向不同 fork；WIP 若直接 push 受保護主分支，也可能被檢查拒絕。Astra review 直接推薦專用 source branch + control ref，卻未說明原專案分支、local commits、PR 與 handoff branch 的關係；這些是採用前的相容性門檻。

**修訂建議：** v1 指定一個權威 remote URL/repository identity 與一條**受管理 publication branch**，所有 handoff 先在這條 branch 上進行。它可以是既有開發分支，也可以是專用分支，但須在初始化時顯式選定；不可從 checkout 或 `origin` 猜測。若採專用分支，Agent 如何把它整合回主線必須在專案 Git workflow 中明定；未整合前不得宣稱主線包含成果。v1 不自動管理 PR。

初始化要驗證 branch 建立/更新權限、受保護分支政策、fetch/push 到同一權威 repository、以及以明確 expected OID 更新 ref 的行為。單 ref 候選不要求 multi-ref atomic push；若改採雙 ref，則**必須另外驗證** remote 支援 atomic multi-ref push 及兩條 ref 的 permissions。Git 文件指出 atomic push 不被所有 server 支援。[Git push documentation](https://git-scm.com/docs/git-push)

**驗收：** fork remote、錯 branch、受保護分支拒絕、remote 被重新指向、local branch 分歧，以及同一 remote 多 push URLs 均不能靜默繞到其他目的地。

### R07 · P1 — Preserve first 缺少可恢復資料的明確範圍

**位置：** §6、§25–26、§38、§48 E、§50 I6；L242–267、L1039–1074、L1428–1430、L1718–1729、L1964–1966。

文件未定義 preservation 是否包含 staged/unstaged changes、untracked files、binary files、local-only commits。backup branch 會漏掉未提交內容；普通 stash 與包含 untracked/ignored 的選項也涵蓋不同資料。[Git stash documentation](https://git-scm.com/docs/git-stash)

**修訂建議：**

- destructive recovery 前驗證 local commits、index、tracked changes、相關 untracked files 能恢復，記錄 recovery handle 與保存範圍。
- ignored files 可能有 secrets 或必要 local data，不能全部上傳或先刪除。可保留原 checkout，以隔離 checkout 恢復遠端狀態。
- checkpoint 使用明確檔案清單，不能無差別 staging。gitignore 也不能代替對已追蹤敏感檔案的檢查。
- local backup 與 remote checkpoint 分開回報；A 無法連線時，B 不得聲稱已保存 A 未上傳的修改。
- preservation 失敗就停止 destructive action，不將 when possible 視為可靜默覆寫的例外。

**驗收：** dirty index、untracked binary、local-only commit、ignored local file 依保存契約保留或明確排除且不刪除；restore 後內容一致，不自動上傳敏感資料。

### R08 · P2 — Minimal state 缺少狀態轉移與參照完整性規則

**位置：** §11–16、§27、§42–45；L437–699、L1078–1124、L1500–1564。

三個 runtime status 可以保留，但需定義有效組合、無 Steps 的 Plan、handoff 後 working 的含義，以及 Plan 修改後 S3 被刪除或重編號的處理方式。current cursor 也不能證明前面所有 Steps 已完成；不必為此新增 progress registry。

**建議契約：**

| 狀態 | 必要規則 |
| --- | --- |
| idle | active_plan = null、current_step = null |
| working | active Plan 唯一存在且為 READY；非 null Step 必須屬於該 Plan |
| blocked | 保留 active Plan/cursor；blocker 為 opaque context |
| ownership released | 不代表 Plan DONE；working 表示可續做工作，不是程序存活 |
| done transition | Plan DONE、cursor 清空、status idle，同一 checkpoint 發布 |
| Plan edit | 同步驗證 cursor；Step ID 不因插入或排序而重新編號 |

允許無 Steps 的 Plan 使用 null cursor；有 Steps 時由 Agent 指定有效 Step，runtime 不自行解讀內容選擇。Plan ID 必須唯一，不能只靠模糊檔名搜尋。為滿足 ID 不重用，最小方案是保留已分配 Plan 文件、禁止刪除後重用；若支援刪除，需額外持久 high-water mark 或其他明確方案。

blocked 的重新開始由 Agent 依專案規則處理；runtime 不重置 debugging policy、不自行開始下一個 Plan。

**驗收：** duplicate Plan IDs、缺失 Step、DONE active Plan、rename、無 Step Plan、Plan 中途修訂、空 repository 初始化都有決定性結果。

### R09 · P2 — Windows/WSL 的執行邊界與支援矩陣未定義

**位置：** §18、§20、§35–37；L742–751、L842–852、L1343–1395。

Windows Agent 操作 WSL checkout 時，哪個環境負責 Git、credentials、paths、local locks 尚未明定。Astra review 建議將 WSL Ubuntu 設為整個 v1 的 canonical runtime；這適合作為**首個驗證環境**，但原產品主張跨機與跨 provider，尚無依據要求所有使用者都必須 WSL。

**修訂建議：** 凍結跨平台的行為契約：每個 checkout 只由一個選定的 runtime/Git environment 操作，session identity 與權威 remote/ref 不依賴 Windows path；本機路徑以該 runtime 正規化。先在 Windows 11 + WSL Ubuntu 驗證，再依實測列出 Windows native、macOS、Linux 的支援狀態。不能把未驗證平台當成已支援，也不宜在 solution design 無故排除它們。

固定 encoding、path validation 與 protocol compatibility。新 protocol 對舊 CLI 拒絕 mutation，保留 raw state 供診斷；credentials 不進 repository。

**驗收：** Windows 啟動、WSL 執行；中文/空白路徑；同 checkout 不同路徑；舊 CLI 讀新 protocol；缺 credentials 均有明確結果。

### R10 · P2 — Control-plane 安全邊界尚未成為協定的一部分

**位置：** §8–9、§32、§36–37、§47；L308–390、L1255–1286、L1366–1395、L1596–1602。

Plan、summary、config 可由前一個 Agent 或 contributor 修改。若 runtime 把 metadata 插入 shell command，或把 summary 的指示視為操作授權，transfer data 就成為執行入口。

**修訂建議：**

- 不執行 summary/Plan 中的指令；以結構化參數呼叫 Git，驗證 refs、paths、IDs。
- 路徑解析後必須仍在預期 repository 範圍，處理 symlink/path traversal，不能只檢查字串 prefix。
- capsule 標示 narrative 為 advisory data，不能覆寫使用者授權、protocol 或 owner identity。
- remote config 變更需驗證，避免任意 repository 修改將 checkpoint 發往新目的地。
- output/logs 不記錄 credentials、含 token 的 remote URL 或 secret file contents。
- approved bootstrap path 應有版本來源與完整性要求，不因缺 runtime 就執行 repository 提供的任意下載指令。

這些屬於 handoff 輸入與執行邊界，不要求新增專案資安掃描引擎。

**驗收：** 惡意 path/ref/summary 不觸發命令執行、越界存取或 credential disclosure。

### R11 · P2 — 保存與復原承諾超出現有資訊

**位置：** §6、§19、§38、§51.1；L255–267、L819–825、L1415–1420、L1990–1995。

§6/§38 使用「最後已驗證 checkpoint」「最安全」，但 §21/§44 明定 checkpoint 不代表 tests passed。沒有 verification evidence 時，只能稱「最後已共享 checkpoint」。

§19 的「最多損失一個小 implementation unit」也只是目標；沒有自動保存或強制 checkpoint 間隔，Agent 可能完成數個 unit 卻未發布。

**修訂建議：**

- 分開回報本機已保存、已共享、驗證狀態，不將 durable 等同 verified。
- 若稱已驗證，由 Coding Agent 提供綁定 revision 的驗證摘要；runtime 只存 opaque evidence，不執行或裁決測試。無證據則 unknown。
- crash guarantee 改為「恢復到最後成功發布的 checkpoint；未發布差距大小取決於 checkpoint 行為」。
- 無 heartbeat 時不以鎖太舊判定 owner 死亡；使用者允許 takeover 不取代 fencing。
- Zero-operation 限定已 provisioning、具備網路/credentials/權限的正常流程；帳號登入與平台審批可能仍需使用者處理。

**驗收：** WIP、tests failed、tests unknown、network down 均不顯示已驗證安全交接。

### R12 · P2 — Bootstrap instructions 無法強制 Agent 進入協定

**位置：** §3、§8、§35–36、§39、§53；L124–143、L308–353、L1343–1375、L1434–1462、L2044–2050。

原文把「新 Agent 自動識別、呼叫 CLI、取得 ownership」寫成產品保證；但 `AGENTS.md`、`CLAUDE.md` 和可選 Skill 是 instruction discovery，無法強制所有 provider、版本或 entry point 一定讀到，也無法阻止忽略協定的 Agent 直接寫檔。這和 R02 的 cooperative 保證邊界必須一致。

**修訂建議：** repository bootstrap 只保留一份 canonical protocol，provider adapter 連到它，並給出最小、確切的 `agentctl status/resume/handoff` 入口。每個支援的 provider/environment 做實際 smoke test：新 session 在第一次修改前能辨識協定、呼叫 CLI、失敗時停止受管理 publication。runtime 不可用時提供可讀的 setup error，不把 CLI 缺失視為可忽略情況。若需要防止所有 raw code edits，須另有強制執行層；v1 將「zero-operation」描述為**已配置且 compliant 的 Agent 的正常體驗**。

**驗收：** 在全新 Codex/Claude session、缺 CLI、舊 protocol、沒有 credentials 的環境各測一次。沒有成功進入協定者不得回報「安全接手」；未驗證的 provider 不列入支援矩陣。

## 3. 對 Astra 方案的再判斷與 v1 建議架構

### 3.1 兩種可行的 publication model

| 方案 | 一致性邊界 | 優點 | 代價 / 前置條件 |
| --- | --- | --- | --- |
| **A. 單一 publication branch（v1 優先驗證）** | branch HEAD 的明確 OID CAS | 一次 ref 更新即含 owner 與完整 project snapshot；不依賴 multi-ref atomic push | acquire/release/takeover 也產生 commit；需要受管理 branch 與隔離 local worktree |
| **B. source branch + control ref（條件式備選）** | 兩條 refs 的同一筆 remote atomic push，control ref 每次實際前進 | source history 不混入純 ownership commits | 要驗證 backend 支援 atomic multi-ref push、兩條 ref 權限及 read consistency；操作、部署及故障面較大 |

選 A 先做原型，是因為 v1 的關鍵不變條件可落在**單一遠端 ref 的前進**，比強制兩個 refs 同步更容易維護。這仍然是一個遠端 Git ownership mechanism：`.agent/owner.json` 與 `state.json` 分開、由同一受管理 branch 的最新 commit 指定。原文 §17/§49.12 所說「不能只靠一般 Git state」需要澄清成「不能只靠未推送檔案或不帶 CAS 的普通 branch 狀態」；單一遠端 ref + conditional update 能提供 cooperative serialization。若產品堅持 ownership 必須位於 source branch 之外，方案 B 才是必要取捨，不能把 A 說成符合此較強的隔離要求。

兩方案都無法強制阻擋持有相同 credentials 的協定外 Git 操作；若要求 adversarial isolation，就需要 server-side policy 或外部授權機制，超出此 Git-only v1 建議。

### 3.2 方案 A 的 canonical state

| 事實 | Canonical location | 約束 |
| --- | --- | --- |
| Current owner | 最新 HEAD tree 的 `.agent/owner.json` | session ID；可為 null；每次變更透過 HEAD CAS |
| Plan intent/status | 同一 HEAD tree 的 Plan 文件 | 僅由 Agent 解讀專案語意 |
| Active Plan/Step/blocker | 同一 HEAD tree 的 `.agent/state.json` | 不保存自身 HEAD OID |
| Recoverable shared snapshot | 權威 publication branch 的 HEAD commit | 這個 OID 是 resume 的 `published_revision` |
| Unpublished local work | 原 checkout 或經驗證的 local snapshot | 不承諾跨機恢復 |

protocol version 以原設計的 `.agent/protocol.json` 為準；owner record 只保存 owner session、generation、最後 operation ID/type，避免兩份版本事實。每次 control-only transition 都建立新的 commit，其 parent 是觀察到的 remote HEAD；即使 project tree 內容不變，也**不得重用同一舊 HEAD**，避免 release/reacquire 後過期操作誤通過。generation 用於可讀的診斷；Git HEAD OID 是真正的 CAS revision。CLI 不把 Plan/status narrative 當成 owner 授權來源。

`last_checkpoint` 若指「目前遠端可恢復的 revision」，從 HEAD 導出即可。若需要顯示最近一次 source 內容變化，將那視為 display metadata，不能讓它取代遠端 HEAD 的恢復基準。control-only commit 保存相同 code/state，但仍是完整可恢復 snapshot。

### 3.3 方案 A 的操作契約

| Operation | 前置條件 | 發布 | 失敗時 |
| --- | --- | --- | --- |
| `status` | 能讀取權威 branch/protocol | 無 mutation；報告讀到的 revision | unavailable/invalid，不 acquire |
| `resume` | owner free 或同意對已觀察 owner takeover | 根據 HEAD K 建 owner commit K′，CAS K→K′ | 不 materialize 舊 snapshot、不回傳 ready |
| `checkpoint` | owner session 正確；local snapshot 有效 | 含 code/Plan/state/owner 的 commit C，CAS current HEAD→C | 保留 local work，核對 unknown outcome |
| `handoff` | checkpoint 前置條件成立 | 一個 commit 保存 WIP/state/summary 並清空 owner | 不先 release 再保存 |
| `release` | current owner session/revision 正確 | control-only descendant commit 清空 owner | 不移除後繼 owner |
| `recover-ownership` | 使用者決定綁定觀察到的 K/owner | descendant commit 換 session/generation，CAS K→K′ | K 改變就重讀，舊決定不得套用到新 owner |
| `done` | Agent 宣告完成、結構驗證通過 | 同一 commit 設 Plan DONE、cursor null、status idle | 不留半完成 shared state |

`resume` 是一次可能修改遠端的操作；read-only session 必須用 `status`/inspect，不應只為讀 Plan 呼叫會 acquire 的 resume。`start` 與 Plan/Step 修改可先在本機準備，但發布前不聲稱已跨機共享。`blocked` 是 project work 狀態；`ownership_conflict`、`publication_unknown`、`ownership_pending` 是 CLI 操作結果，不寫成持久 project blocker。

### 3.4 線性化點與失敗處理

```text
Acquire checkout-local command lock
Read authoritative remote HEAD K and validate its owner/protocol
Preserve unrelated local work; choose an isolated/managed checkout if needed
Create one descendant commit C containing the entire new snapshot
Validate K is an ancestor of C; pin explicit expected OID K
Conditionally push C to the one publication ref
Observe remote ref after success, rejection, or lost reply
Materialize the committed snapshot only after the intended transition is known
Return published / rejected / publication_unknown / ownership_pending
```

使用明確 refspec 與 `--force-with-lease=<ref>:<K>` 作 compare-and-swap，**再自行驗證 ancestry**，因 lease 本身可允許 non-fast-forward。不得從 background-updated remote-tracking branch 取隱含 expected 值。[Git push documentation](https://git-scm.com/docs/git-push)

read-only client 抓取一個 HEAD OID，所有 Plan、state、owner 從該 commit 讀；若要宣稱「目前最新」，需再次核對 remote HEAD。Git fetch 與 working tree 更新分開；未經驗證不得 reset/clean/rebase 既有 local work。network timeout 後先核對預先計算的 commit OID 與 operation ID，仍無法判定就保持 unknown。

### 3.5 對原 review 強方案的保留條件

方案 A 初始化時應從選定的 project base 建立受管理 branch，使用預期「遠端 ref 不存在」的條件式建立；若 branch 已存在就讀取並驗證，不覆寫。control-only commits 會留在該 branch 歷史，屬協調成本，不應誤作 implementation checkpoint；如何合併至主線及下一個 Plan 如何吸收主線變更，需先在專案 Git workflow 寫明。若此成本不可接受，才考慮方案 B。

方案 B 只有在 source/control 必須分開或純 ownership commits 無法進入受管理 branch 時才值得採用。若選 B，每次 checkpoint/handoff 必須讓 source ref 與 control ref 在**同一 remote transport transaction** 成功或失敗；兩個 refs 各用明確 expected OID，control ref 必須產生新值。acquire/release 為 control-only CAS。不能以先推 source、再推 owner 補救，也不能拿本機 `git update-ref` transaction 代替 remote atomicity。多 push URLs/remote group 不提供跨 remote atomicity。[Git push documentation](https://git-scm.com/docs/git-push)、[Git update-ref documentation](https://git-scm.com/docs/git-update-ref)

雙 ref read-only 若先抓 control K、再抓 source，兩次 fetch 不構成同一 snapshot；若兩者暫時不匹配，應重新抓穩定 control revision 判斷並重試，不能立刻認定 repository 損毀。若 hosting backend 不支援所需 atomic/ref policy，方案 B fail closed，回到設計決策，不自動降級成無 fencing 的兩次 push。

## 4. 實作前應凍結的決策

| 必須現在決定 | 可繼續延後 |
| --- | --- |
| cooperative guarantee 與 raw Git bypass 邊界 | implementation language / package layout |
| 方案 A 或 B、權威 remote/ref、既有 branch/PR 如何整合 | ref/command 的最終字面名稱 |
| CAS publication、owner session、takeover/release 語意 | Git plumbing 的程式封裝 |
| snapshot/HEAD 與 WIP checkpoint 的關係 | 完整 JSON/YAML 欄位排版 |
| timeout、unknown result、retry 契約 | exit code 的實際數字 |
| dirty worktree 保存範圍、隔離 checkout 行為 | diagnostics 顯示格式 |
| protocol mismatch、backend 不相容行為 | installer packaging |
| Plan/Step 結構不變條件 | 更完整 Plan 模板 |

§55 可以延後 implementation details，但不能一起延後 safety semantics。最小原型若證實方案 A 與現有 Git workflow 不相容，應明確改選並驗證方案 B，或收窄 v1 支援環境。

## 5. 必要驗收矩陣

以下為未來的完整驗收要求，**本次沒有執行整套測試**。本次僅完成 T02 的最小 Git CAS 機制試驗（見 §7）。實作時仍需在 WSL 以 local bare remote + 兩個獨立 clones 建立可重複 integration tests，再測選定的 hosting backend。

| ID | 場景 | 必須成立 |
| --- | --- | --- |
| T01 | A/B 同時 acquire | 只有一個成功 |
| T02 | A check 後暫停，B takeover，A push | A 整筆 publication 被拒絕 |
| T03 | 舊 A 在 B acquire 後 release | B ownership 保留 |
| T04 | 各 commit/push/release 邊界 crash | 只恢復完整 checkpoint |
| T05 | server 成功但 client 收不到回覆 | 核對原 operation，不盲目重做 |
| T06 | B fetch 後 A final handoff | B resume 最新 publication |
| T07 | 同 owner 兩個 checkpoint | 序列化或決定性衝突，不混用 staging |
| T08 | dirty index、untracked binary、local commits | 不丟失承諾保存的 bytes |
| T09 | A 離線、B takeover、A 上線 | A 工作可保留但不能直接發布 |
| T10 | 方案 A ref update 被拒；方案 B atomic 不支援 | fail closed；無部分 publication |
| T11 | Plan rename/delete/duplicate、Step mutation | 按契約接受或拒絕，不猜測 |
| T12 | WIP / tests failed / unknown evidence | durable 與 verified 不混淆 |
| T13 | read-only inspection | 不 acquire、不改 working tree/state |
| T14 | newer protocol、malformed state、path traversal | 拒絕 mutation，不執行輸入 |
| T15 | fresh repository、並行 init、首次 checkpoint、Plan 完成 | ref 不覆寫；null 與初始化路徑一致 |
| T16 | 協定外寫入 remote/source ref | 偵測不一致並停止，不覆寫 |
| T17 | Windows Agent 呼叫 WSL runtime | identity、paths、credentials 一致 |
| T18 | takeover 決定過期後重試 | 不能奪取另一個新 session |
| T19 | 單 ref control-only acquire/release | project snapshot 不變，HEAD 每次前進 |
| T20 | 協定外改寫/刪除 publication branch | 偵測失效，不宣稱有可恢復 checkpoint |
| T21 | 新 provider session / 缺 CLI | 未進入協定不得回報安全接手 |
| T22 | publication branch 與 main 分歧或完成整合 | 明確處理後續 Plan 的基底，不丟已發布工作 |

## 6. 原文修訂定位

| 原文章節 | 建議更新內容 |
| --- | --- |
| §4.3、§17、§50 I1–I3 | cooperative publication、session identity、單 ref CAS；雙 ref 才需 atomic publish |
| §15、§19、§21、§45 | 移除 self-reference 歧義，統一 snapshot/publication/completion |
| §23、§39、§48 A | 統一 resume 是否含 acquisition；補重新核對與 materialization |
| §25、§35、§42 | 權威 remote/ref、branch 整合、backend capability、dirty tree、setup errors |
| §6、§26、§38、§48 E | preservation 成功條件，修正無證據的已驗證描述 |
| §11–14、§43 | Plan/Step identity、有效組合、mutation validation |
| §37、§46–47 | protocol mismatch、structured result、unknown outcome、idempotency |
| §49.16、§49.17、§49.24 | 改標待交易協定及測試驗證，避免誤認已解決 |
| §51.1 | 將最多損失一個 unit 改為 cadence 目標 |
| §8、§39、§53 | provider bootstrap smoke test 與 zero-operation 的適用邊界 |
| §55–56 | 將 safety semantics 提升為設計凍結前必要決策 |

## 7. Review 範圍與證據

已重新檢視原設計與先前 Astra review 的產品範圍、bootstrap、Plan/Step、ownership、checkpoint、resume、Git/recovery、CLI、版本相容性、UX、invariants 與 deferred scope。未取得前一版文件、實作、部署設定或 hosting configuration，未驗證 migration 與實際 backend 能力。

競態與失敗時序主要是根據文件契約推導的架構分析；提案未視為完成實作。本次另在 WSL Ubuntu（Git 2.52.0）的暫存 bare remote 與兩個 clone 做單一 ref 試驗：A acquire 後，B 以 A 的 OID 為 expected 推送 takeover commit；A 再以舊 OID 嘗試 push，遭拒絕，remote owner 仍為 B。這只驗證該局部 CAS 情境，沒有驗證 timeout、hosting policies、raw push 防護或整套 runtime。Git primitive 行為參照官方文件：

- [git-push](https://git-scm.com/docs/git-push)：atomic push、explicit expected-value lease、remote refs。
- [git-update-ref](https://git-scm.com/docs/git-update-ref)：repository ref compare-and-update / transaction。
- [git-stash](https://git-scm.com/docs/git-stash)：index/working tree 與額外檔案保存範圍。

下一個里程碑建議為 **單 ref ownership + fenced publication + crash recovery 最小原型，通過 T01–T06、T09–T10、T18–T20**。若和選定 Git workflow 不相容，再以雙 ref 方案進行有條件比較；最後補 Plan UX 與 provider bootstrap。
