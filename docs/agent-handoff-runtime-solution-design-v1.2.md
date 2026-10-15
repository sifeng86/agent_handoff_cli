# Agent Handoff Runtime
## Solution Design v1.2 — Protocol Closure

- 日期：2026-10-02
- 狀態：implementation-ready design specification；仍需依本文件完成 prototype 與 acceptance tests 後，才可宣稱 runtime 已驗證。
- 取代：v1.1 作為後續實作的唯一設計基準；保留 v1.0/v1.1/review 供歷史比較，但實作者不得依賴舊文件補足本版語意。
- 核心方向：sequential Agent handoff、single publication branch、explicit CAS、session fencing、本機 `.env` 優先、Doppler 共享。
- 首個驗證環境：Ubuntu，包括 Windows 11 + WSL Ubuntu。

---

## 0. 規範語言與實作原則

本文件中的 **MUST / MUST NOT / SHOULD / SHOULD NOT / MAY** 為 normative requirements：

- **MUST / MUST NOT**：安全或正確性必要條件；不符合即不得宣稱相容。
- **SHOULD / SHOULD NOT**：強烈建議；偏離時必須有明確、可測試的理由。
- **MAY**：可選實作，不影響協定相容性。

本版的首要原則：

1. **Runtime 只管理 handoff/control-plane safety，不管理專案語意。**
2. **Agent 操作工具，使用者只表達意圖與必要決策。**
3. **所有 mutation 必須建立在已觀察的 authority 與可驗證前置條件上。**
4. **無法證明安全時停止 mutation、保存本機資料、回報可執行決策；不得猜測。**
5. **本機未發布資料優先保存；遠端 authority 不因本機猜測而被覆寫。**
6. **Git 與 secrets 系統沒有跨服務 atomic transaction；必須分開回報。**

---

## 1. 產品與操作體驗

此 template 讓不同電腦、帳號與 coding providers 依序接續同一個開發專案。正常情況由 coding Agent 撰寫程式、操作 Git、維護 Plan、保存 checkpoint、操作 `agentctl`，以及處理已授權的環境設定同步。使用者以自然語言下達工作、停止與交接要求。

| 使用者說法 | Agent 行為 |
| --- | --- |
| 繼續開發 | 唯讀檢查 → 保留本機修改 → 判定 ownership → acquire 或 recover-session → materialize → 驗證環境 → 繼續 |
| 今天先做到這裡 | 固定本機 snapshot → 嘗試已授權 secrets reconciliation → handoff commit 發布 code/Plan/state/summary 並清空 owner |
| 我手動改了檔案，請保留並繼續 | 保留修改、比對與整合；project semantic conflict 由 coding Agent 的一般 workflow 處理 |
| 我更新了 `.env`，請同步設定並繼續 | 三方比對 B/L/R；無衝突則依授權同步；runtime-level conflict 才使用 decision flow |
| 接手另一個 Agent 的工作 | 讀取 authority；若 owner 仍有效且無法證明為同一 session，使用 takeover decision flow |

### 1.1 使用者不需要操作 CLI

正常操作中：

- Agent **MUST** 自行操作 `agentctl`、Git、Doppler adapter 與必要 shell commands。
- Agent **MUST NOT** 把 recovery procedure 轉嫁成「請使用者執行某個 CLI 指令」。
- 需要人類選擇時，Agent **MUST** 以自然語言列出有限、可理解的選項；使用者選擇後由 Agent 自行執行。
- 只有 Agent 無法代理的外部人類驗證，例如首次帳號建立、瀏覽器登入、OAuth consent、2FA、硬體金鑰或平台明確要求的人工授權，才可要求使用者直接操作該驗證步驟。
- 使用者不需要知道 `agentctl` 的命令名稱、flags、Git refspec 或 Doppler CLI 語法。

---

## 2. 範圍、非目標與保證邊界

### 2.1 v1.2 管理的範圍

- sequential handoff control plane；同一 authority 同時最多只有一個合規 writer session。
- ownership acquire / recover-session / takeover / release。
- fenced publication、checkpoint、handoff、done。
- crash / timeout / unknown publication outcome recovery。
- 本機未發布資料保留與 managed checkout materialization。
- Plan/Step control-state integrity。
- local `.env` ↔ Doppler shared config reconciliation。
- runtime/control-plane decision-only human intervention。

### 2.2 明確非目標

Runtime **MUST NOT**：

- 同步聊天紀錄。
- 安排多 Agent 平行開發。
- 判斷 feature 是否應做、架構選擇、測試是否合理、某個 bug 如何修、產品需求優先級等專案語意。
- 自動決定下一個 Plan。
- 自動管理 PR lifecycle。
- 將 `agentctl` 擴張成通用 secret manager。
- 假裝能阻止 raw Git push、外部 editor、未遵守 instructions 的 Agent 或 hosting administrator。

### 2.3 保證邊界

- Runtime 保證的是**遵守協定的 writer** 之間的 fencing 與 publication correctness。
- 沒有 heartbeat 或 autosave daemon；owner 的年齡不是 session 死亡證據。
- 最後成功發布的 Git snapshot 是跨機 recovery baseline；另一台機器未發布的資料不保證可恢復。
- 外部 writer 可能導致 CAS rejection 或 integrity violation；runtime 不得用普通 force push 蓋回去。

---

## 3. 架構與事實來源

| 資料 | Authority |
| --- | --- |
| 可交接的程式、Plan、進度 | 權威 publication branch 的同一 commit |
| Current owner | 該 commit 的 `.agent/owner.json` |
| Protocol version | `.agent/protocol.json` |
| Active Plan、Step、blocker | `.agent/state.json` |
| 功能意圖與完成狀態 | `plans/` 中的 Plan 文件 |
| Operation identity in a published commit | commit 內的 non-secret operation metadata |
| 本機 session capability / binding | Git 外的 user-private runtime state |
| Pending/unknown operation journal | Git 外的 user-private runtime state |
| 本機執行所用 secrets | 本機 `.env` 與本機 credential files |
| 跨機共享的 secret 值 | 明確綁定的 Doppler project/config |
| 上次 secret 同步基準 B | Git 外的受保護本機資料 |
| Provider 登入憑證 | 各機器自己的 provider credential store |

### 3.1 Repository identity

Runtime **MUST** 建立穩定的 repository identity，至少綁定：

- canonical fetch/push destination identity；
- publication ref；
- hosting/provider identity；
- 必要時 repository immutable identifier。

不得只依目前資料夾名稱、`origin` 別名或 human-readable project name 判定 repository identity。

### 3.2 Git 與 Doppler 是獨立系統

Git 與 Doppler 沒有跨服務 atomic transaction：

- code 已 shared 不代表 secrets 已 shared；
- secret sync 成功不代表 code checkpoint 成功；
- 每次 handoff **MUST** 分開回報 `code_publication` 與 `secrets_shared` 狀態。

---

## 4. Template 與本機私有資料配置

### 4.1 Repository 內

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

`AGENTS.md` 與 `CLAUDE.md` SHOULD 指向同一份 canonical instructions，避免 protocol 內容漂移。

Repository **MUST NOT** 包含：

- 真實 secrets；
- provider login tokens；
- 本機 session capability；
- secret baseline；
- pending operation journal；
- 作者 workplace-specific binding。

### 4.2 Git 外 user-private runtime state

至少包含：

```text
<private-root>/
  repos/<repository-identity>/
    sessions/<session-id>.json
    locks/
    operations/<operation-id>.json
    decisions/<decision-id>.json
    bindings.json
    secrets/<config-identity>/baseline.dat
```

實際路徑可依平台調整，但：

- private root MUST 只有目前使用者可讀寫；Unix 類平台建議 directory `0700`、敏感 file `0600` 或更嚴格。
- session/operation/baseline data **MUST NOT** 位於一般 checkpoint 會掃描的 repository path。
- Runtime **MUST** 驗證 path traversal 與 symlink escape。

---

## 5. 初始化、Ubuntu/WSL 邊界與 Host Preservation

Agent 負責確認 repository、Git、CLI、remote 權限與 runtime environment。每個 checkout 固定由一個 runtime/Git environment 操作；WSL checkout 的 Git、credentials、paths、locks 全部在 WSL 處理。

初始化必須明確選定：

1. authority repository identity 與 fetch/push destination；
2. publication branch、初始 project base 與主線分支；
3. Doppler project/config、shared key 清單與 local-only keys；
4. 本機 `.env` 位置、credential file mappings 與環境需求。

### 5.1 不得猜測 authority

- MUST NOT 從 `origin` 名稱或目前 branch 猜測 authority destination。
- MUST reject 多個互相衝突的 push URLs。
- Provider/config binding 變更需要明確意圖；不得因 repository 檔案被改動就把 secrets 發往新目的地。
- 建立 publication branch 時使用 expected-absent conditional update；已存在就讀取、驗證，不覆寫。

### 5.2 Host preservation guardrail

Agent 在安裝或修復 `agentctl`、Git、Doppler CLI 或 runtime dependencies 時：

- **MUST inspect first**，先理解現有環境與衝突來源。
- **MUST preserve existing behavior by default**。
- **MUST NOT** 任意 disable、overwrite、delete 或重設既有：
  - services；
  - firewall/network configuration；
  - VPN configuration；
  - SSH access；
  - users/groups；
  - unrelated packages；
  - package repositories；
  - shell/profile configuration；
  - Git/global Git config；
  - Codex/Claude/provider configuration；
  - credentials；
  - system security policy。
- 必要變更 **MUST** 採 minimum-change 原則。
- 任何可能 destructive/replacement 的必要改動前 **MUST** 建立可驗證 backup/recovery handle。
- **MUST NOT** 做可能鎖住使用者、破壞網路、VPN、SSH 或其他既有工作流程的變更，除非該變更是使用者明確要求且有 recovery plan。
- 發現與 Surfshark、其他 VPN、proxy、DNS、firewall 或 enterprise policy 可能衝突時，先診斷與隔離，不得把停用既有網路/security service 當成預設修法。

---

## 6. Single Publication Branch 與 CAS Publication

Owner、code、Plan、state、summary 使用同一 publication branch。這是 v1.2 唯一支援的 publication topology。

每次 acquire、takeover、checkpoint、handoff、release、done 都建立新 commit，並以**當次操作實際讀到的遠端 HEAD OID** 作為 compare-and-swap 前置條件。

Control-only commit 可只改 owner/control metadata；它仍是完整可恢復 snapshot，但不代表新增 implementation 或重新通過測試。

### 6.1 最小 publication contract

每個 mutation MUST：

1. 取得 repo/session-scoped mutation lock 與 checkout-local command lock。
2. 直接讀取 authority ref HEAD = `K`；不得使用可能被背景 fetch 改變的隱含 tracking ref 當 expected OID。
3. 從 `K` 讀取 protocol/owner/state 與必要 snapshot metadata。
4. 驗證 session、operation intent、state invariants 與 integrity preconditions。
5. 建立 commit `C`，parent 明確為 `K`。
6. 一般 control operation 不得偷偷帶入 unrelated local commits。
7. 驗證 `parent(C) == K` 與必要 ancestry。
8. 使用明確 refspec 與 equivalent conditional update；Git CLI 實作可使用 `--force-with-lease=<ref>:<K>`。
9. 核對發布結果；timeout/reply loss 時進入 operation recovery，不盲目重送。
10. **MUST NOT** 以普通 `--force`、刪除 ref 後重建等方式兜底。

### 6.2 CAS failure

CAS failure 代表前置事實已改變：

- MUST 重新讀取 authority。
- MUST 重新評估原操作是否仍成立。
- MUST NOT 自動只替換 expected OID 後重送舊 intent。
- 若變化只是正常 descendant commit，可依最新 state 重新建立新的 operation。
- 若 authority history 被 rewrite/delete/non-descendant replacement，進入 Section 7 的 integrity violation flow。

---

## 7. Publication Integrity Violation

以下任一情況 MUST 分類為 `publication_integrity_violation`，不是普通 concurrency：

- authority publication ref 被刪除；
- observed new HEAD 不是先前已知 authority HEAD 的 descendant，且 runtime 沒有可證明的合法 protocol transition；
- publication history 被 force-rewrite；
- protocol/owner metadata 被外部修改到不符合 schema/invariant；
- authority destination identity 在未授權情況下改變。

### 7.1 違規時的行為

Runtime MUST：

1. 停止所有 publication mutation。
2. 保留本機 dirty work、local commits、ignored/untracked files，不 reset/clean。
3. 允許 readonly inspect/status。
4. 記錄 observed ref/OID/owner 與 violation reason，不記 secret values。
5. 建立 control-plane decision envelope。
6. 只有使用者做出仍對當前 observation 有效的決策後，才可進行 adopt/restore/abort 類動作。

Runtime MUST NOT：

- 自動 recreate 被刪除的 publication ref；
- 自動把未知 non-descendant HEAD 當成新的可信 authority；
- force-push known-good history 覆蓋遠端；
- 因為「看起來像自己的 commit」就忽略 integrity violation。

### 7.2 可提供的決策選項

依情境可包含：

- **Adopt current remote authority**：保留本機資料，重新以當前遠端作基準；
- **Restore known-good authority**：只在 hosting policy、權限、backup/recovery evidence 都足夠時執行；
- **Abort managed handoff**：停止 runtime mutation，保留本機資料供人工/其他 workflow 處理。

選項不是固定三個；Agent MUST 依實際可安全執行的動作產生，不得提供做不到的選項。

---

## 8. Ownership 與 Session Model

### 8.1 Repository owner state

`.agent/owner.json` 的邏輯狀態只有：

- `owner = null`：UNOWNED；
- `owner.session_id = <opaque-id>`：OWNED。

Machine/provider labels 只供診斷，**不是授權證據**。

Generation MAY 存在供人類閱讀與 debug，但 publication fencing 的 authority 是 HEAD OID + session identity，不是 generation number。

### 8.2 Session ID

Session ID MUST：

- cryptographically unguessable 或具有等效 collision resistance；
- 一旦產生不可重用；
- 不從 machine name/provider name/time narrative 推導；
- 不因 Agent process restart 就任意重建成同一值；
- 不得只靠 repository 中的 `owner.json` 讓任意新 process 自稱為 owner。

### 8.3 Local session capability record

成功 acquire 或 takeover 後，runtime MUST 在 Git 外保存 local session record，至少綁定：

- repository identity；
- publication ref；
- session ID；
- acquisition operation ID；
- acquisition/resulting authority OID；
- provider/runtime diagnostic identity；
- managed checkout identity；
- record schema/protocol version。

Local session record 是證明「這個 runtime process family 可合法恢復同一 session」的 capability material。其內容不得提交 Git。

### 8.4 Session recovery semantics

`recover-session` 只有在以下條件全部成立時可無需 human takeover decision：

1. 本機存在有效、未撤銷 session record；
2. repository identity/ref 完全匹配；
3. authority owner.session_id 仍等於該 session ID；
4. authority history 與 acquisition/recovery evidence 可驗證；
5. 沒有另一個本機 write-capable checkout 正使用同一 session；
6. 沒有 pending integrity violation 或 stale operation ambiguity。

若 local session record 遺失、損壞或無法驗證，runtime **MUST NOT** 只靠遠端 owner.json 自我恢復 ownership。此時若 owner 非 null，視為「owned by other/unprovable session」，需要 takeover decision。

### 8.5 同一 session 的 write capability

同一 session 同時只允許一個 write-capable managed checkout。

- Runtime MUST 使用 repository/session-scoped local lock 防止同機兩個 checkout 同時以同一 session mutation。
- 其他 checkout MAY readonly inspect。
- 如果無法取得 repo/session lock，MUST NOT 執行 mutation。

---

## 9. Ownership Operations 與 State Machine

### 9.1 Vocabulary

本版統一使用：

| 操作 | 定義 |
| --- | --- |
| `status` / `inspect` | 純唯讀；不 acquire、不 materialize、不更新 working tree |
| `acquire` | authority owner 為 null 時建立全新 session 並取得 ownership |
| `recover-session` | 以本機 capability 恢復**同一個既有 session**，不換 owner identity |
| `resume` | user-facing orchestration：inspect → preserve → acquire 或 recover-session → materialize → validate → ready |
| `takeover` | 以**新 session** 取代目前另一個/不可證明的 owner；需要 valid human decision |
| `checkpoint` | 由有效 owner 發布完整可恢復 Git snapshot；不釋放 owner |
| `handoff` | 同一 commit 保存 code/Plan/state/summary 並清空 owner |
| `release` | 有效 owner 發布 control-only owner=null；不代表 Plan 完成 |
| `done` | 原子更新 Plan DONE、cursor null、state idle；是否同時 release 由明確 intent 決定 |

`recover-ownership` 這個 v1.1 名稱在 v1.2 **移除**，避免混淆 `recover-session` 與 `takeover`。

### 9.2 Acquire

Preconditions：

- owner == null；
- authority integrity valid；
- no unresolved publication_unknown mutation that could have changed ownership；
- repo/session local lock acquired。

Effect：

- 建立 new session ID；
- 建 acquisition commit `K'` parent=`K`；
- CAS publish；
- 成功核對後才建立/activate local session capability record。

兩個 concurrent acquire 最多只能有一個對同一 expected `K` 成功。

### 9.3 Recover-session

- 不建立新 owner identity。
- 不發布「假 acquire commit」只是為了證明自己存在；是否需要 control commit 由具體 recovery state 決定。
- 成功條件必須符合 Section 8.4。
- 若 authority owner 已不同，local session 立即視為 fenced，不能自行改成新 owner。

### 9.4 Takeover

Takeover 的目的：新 session 取代現有 owner。

Preconditions：

1. observed authority integrity valid；
2. owner != null；
3. 目前 runtime 無法合法 recover 為該 owner session；
4. 建立 takeover decision envelope，至少綁定 observed HEAD + observed owner.session_id；
5. 使用者選擇 takeover；
6. 執行前重新讀取 authority，decision 仍未 stale。

Effect：

- 建立**新的**不可重用 session ID；
- CAS commit 由 observed `K` 前進到 `K'` 並寫入 new owner；
- 舊 session 立即被 fencing；
- 舊 session 的 checkpoint/release/handoff 必須因 owner mismatch 被拒絕。

Takeover MUST NOT 自動刪除舊 session 的本機資料。

### 9.5 Release

只有 authority owner.session_id 與 local valid session 相同時才能 release。

舊/fenced session 的 release **MUST NOT** 清除新 owner。

### 9.6 Handoff

Handoff MUST 在同一 publication commit 中：

- 固定並發布本次 Git snapshot；
- 更新 Plan/state/summary；
- owner 設為 null；
- 寫入 non-secret operation identity。

不得先 checkpoint 再以另一個可能失敗的 commit release，並宣稱已完成 atomic handoff。

### 9.7 Done

Done 的 state transition 必須原子滿足：

- active Plan → DONE；
- `current_step = null`；
- runtime state → idle；
- related references valid。

Owner 是否同時 release 是獨立明確 intent；不得從 `done` 字面自行猜測。

---

## 10. Operation Journal 與 Crash Recovery

每次 mutation 在對遠端寫入前 MUST 建立本機 operation journal：

```text
operation_id
operation_type
repository_identity
publication_ref
expected_oid
prepared_commit_oid
payload_identity
session_id (if applicable)
created_at (diagnostic only)
status
```

Operation metadata 亦 SHOULD 以 non-secret form 出現在 prepared/published commit，供 reply loss 後核對。

### 10.1 Operation result states

| 狀態 | 意義 |
| --- | --- |
| `published` | 能證明該 operation 的 intended commit 已發布；不代表 session 目前仍持有 ownership |
| `rejected` | authority 明確拒絕；必須重新評估 |
| `publication_unknown` | 無法證明成功或失敗 |
| `local_only` | 本機 snapshot/journal 已保存，遠端尚未共享 |
| `ownership_pending` | acquire/takeover/release 的最終 authority 狀態尚未證明 |

### 10.2 Unknown outcome recovery

遇到 push timeout、connection drop、client crash、reply loss：

1. MUST 先讀取 authority ref；
2. 檢查 operation ID / prepared commit 是否已出現在 authority history；
3. 若能證明已發布，標為 `published`，不得再次發布第二筆等價 operation；
4. 若能證明未發布且 authority 仍等於 expected OID，可明確地重新執行同一 prepared operation 或建立新的 operation，依 backend contract 決定；
5. 若無法證明，保持 `publication_unknown`，停止會依賴該結果的 mutation；
6. MUST NOT 盲目生成第二個 owner-changing commit。

---

## 11. Decision-Only Human Intervention Contract

### 11.1 嚴格範圍

Decision-only human intervention **只屬於 Agent Handoff/runtime control plane**。

允許使用此機制的例子：

- takeover 既有 owner；
- publication integrity violation 的 adopt/restore/abort；
- authority repository/ref/provider binding 改變；
- secret 同 key 衝突；
- secret deletion；
- shared destination/config 改變；
- Production 或擴大 secret sharing scope 的授權；
- 其他若 runtime 自行決定會造成 ownership/publication/secret safety 風險的情況。

### 11.2 禁止擴張到 Project Semantics

以下 **MUST NOT** 進入 agentctl/runtime decision mechanism：

- 測試檔本身是否有 bug；
- feature 應不應新增；
- 是否加入 Google Home；
- 演算法怎麼設計；
- API/schema/product behavior 的一般衝突；
- code style、refactor、architecture choice；
- acceptance criteria 本身是否合理；
- 一般專案優先級與產品判斷。

這些問題仍由 coding Agent 的正常 project workflow 處理。Coding Agent可以依專案需要詢問使用者，但**不得把它包裝成 Agent Handoff runtime 的 takeover/safety decision**，也不得讓 `agentctl` 自己裁決。

### 11.3 Decision envelope

每個 runtime decision MUST 有 immutable decision envelope，至少包含：

```yaml
decision_id: opaque-id
category: takeover | publication_integrity | secret_conflict | secret_deletion | authority_binding | other_control_plane
repository_identity: ...
publication_ref: ...
observed_head: <oid-or-null>
observed_owner_session: <session-id-or-null>
destination_identity: <non-secret-id-or-null>
reason: <non-secret summary>
options:
  - option_id: ...
    effect: <non-secret description>
invalidated_by:
  - authority_head_change
  - owner_change
  - destination_change
  - relevant_secret_state_change
```

Decision MAY 增加其他 non-secret preconditions，但不得存秘密值。

### 11.4 Stale-decision invalidation

使用者選擇 option 後、任何 mutation 前 MUST 重新讀取 relevant authorities。

以下任一事實改變時，舊決策自動失效：

- publication HEAD 改變；
- owner 改變；
- authority destination/ref 改變；
- secret conflict 涉及的 B/L/R relevant state 改變；
- option 的安全前置條件不再成立。

Runtime 必須重新計算並重新提出 decision；不得把舊同意套用到新狀態。

### 11.5 User-facing decision UX

Agent MUST：

- 說明發生什麼事；
- 只顯示 non-secret facts；
- 提供少量、可安全執行的 options；
- 說明每個 option 的直接效果；
- 使用者只需要選 option，不需要操作 CLI。

Agent MUST NOT：

- 要求使用者自行 resolve Git ref/lock；
- 顯示 secret values；
- 以模糊的「要不要強制？」取代具體效果；
- 提供會破壞未保存本機資料的 option 卻不先建立 recovery handle。

---

## 12. Resume、Materialization 與本機資料保留

Discovery 先只讀 metadata。Resume 不把之前 fetch 的 working tree 當成必然最新。

### 12.1 Resume orchestration

1. 唯讀檢查 authority metadata。
2. 檢查 dirty index、tracked/untracked changes、local-only commits、ignored files、merge/rebase state。
3. 建立可驗證 preservation/recovery handle；必要時保留原 checkout 不動。
4. 判定：
   - owner null → `acquire`；
   - owner == local provable session → `recover-session`；
   - owner != local provable session → decision → `takeover` 或停止；
   - integrity violation → Section 7。
5. 只有成功 acquire/recover-session 後，才建立/更新 execution managed checkout。
6. 驗證 Plan/Step/schema、本機修改整合狀態與必要環境。
7. 再次確認 authority owner/session 未變。
8. 滿足可執行條件後才回報 `ready`。

### 12.2 Materialization failure

若 acquire/takeover 已成功但 materialization 失敗：

- 優先使用相同有效 session/revision 嘗試 release；
- release outcome unknown 時回報 `ownership_pending`；
- 不因本機 checkout 建立失敗就假裝 owner 已釋放。

### 12.3 Preserve-first

- MUST NOT 自動 `reset --hard` / `clean` 原 checkout。
- ignored files 不自動刪除、上傳或搬移。
- destructive recovery 前 MUST 有 recovery handle 與明確保存範圍。
- 保存失敗即停止該 destructive action。

---

## 13. 使用者手動修改與外部 Editor

每次開始/繼續工作及 checkpoint 前，Agent 檢查 diff。

- 同一 checkout：納入檢查過的修改，不覆蓋使用者意圖。
- 另一個 checkout：保留原內容，在 managed checkout 比較與整合。
- 另一台未同步機器：runtime 無法自動發現；不得宣稱已保存其內容。
- Snapshot 固定後的新修改保留在本機，不得被此次 commit 誤標為已 shared。
- 同機非合作 editor 可能持續修改；local lock 不能阻止外部 editor。

### 13.1 Project semantic conflicts

若手動修改與目前 implementation goal 在語意上衝突：

- coding Agent 依專案 workflow 處理、分析、必要時詢問使用者；
- `agentctl` 只確保資料不被無意覆蓋；
- **不得**把一般 code/product conflict 升級為 runtime takeover/decision-only human intervention。

### 13.2 外部修改 publication branch

- 正常 descendant external commit → CAS fail 後重新讀取完整 snapshot 並重新評估。
- non-descendant rewrite/delete/invalid protocol → Section 7 integrity violation。
- 手改 protocol/owner 不會產生合法 session capability。

---

## 14. Plan、Step 與完成狀態

Plan 文件包含：

- 唯一且不可重用的 Plan ID；
- Goal；
- Scope；
- Non-goals；
- Steps；
- Acceptance Criteria；
- Status。

Plan status 僅：`DRAFT`、`READY`、`DONE`。

| Runtime state | 不變條件 |
| --- | --- |
| `idle` | active_plan=null、current_step=null |
| `working` | active Plan 存在且 READY；current_step 若非 null 必須存在於該 Plan |
| `blocked` | 保留有效 active Plan/cursor；blocker 只是 advisory context |
| owner released | 不代表 Plan 完成；`working` 可留待下一 session |
| done transition | Plan DONE + cursor null + state idle 原子發布 |

規則：

- Step ID 不因排序或插入而重編號。
- 無 Steps 的 Plan 可用 null cursor。
- 有 Steps 時由 coding Agent 明確選 current step；runtime 不猜下一步。
- Plan 刪除/改名/變更必須同步驗證 references。
- 已分配 Plan ID 不重用。
- Checkpoint 不等於 acceptance passed。
- 測試摘要必須綁定受測 revision 與 test scope；沒有 evidence 就標 `unknown`。
- blocker 內容不賦予 runtime project decision authority。

---

## 15. 本機 `.env` 優先與 Doppler 共享

### 15.1 模型

所有一般 token、API keys、secret configuration 以本機 `.env` 作為日常編輯與執行入口；Doppler 保存跨機共享副本。

「本機優先」只代表 editing/runtime entry point，不代表任何舊本機值有權覆蓋雲端。

每次 reconciliation 比對：

- `B`：上次成功同步基準；
- `L`：本機當前值；
- `R`：遠端 shared config 當前值。

無本機檔案的新機器可由 R 建立本機內容。已有 `.env` 但沒有 B 的機器必須先做 initial reconciliation。

### 15.2 授權與初始化

- 每個開源使用者建立自己的 Doppler project；同一專案的機器指向明確選定的 shared config。
- 每台 Ubuntu 完成必要 CLI 安裝與登入。
- 自動上傳 writer 必須有更新 selected config 的權限；read-only credential 不足。
- 首次啟用共享模式時，可一次授權 shared key scope 的新增/修改同步。
- 若沒有 persistent authorization，「請同步 `.env`」可授權此次同步。
- Production、刪除、改 destination、擴大 sharing scope 必須有明確 control-plane intent/decision。
- 不要求使用者把真實 secrets 貼到對話。

### 15.3 Key classification

Manifest 區分：

- `shared`；
- `local-only`；
- `file-backed`。

只同步允許的 shared keys。

不得上傳：

- machine-specific paths；
- provider login token；
- local overrides；
- 未分類未知 key。

不得無差別 import 整份 `.env`。

### 15.4 Dotenv parsing

- 使用 dotenv parser，不以 shell source/eval 執行 `.env`。
- v1.2 至少支援 UTF-8、明確 quoting 與字串值。
- duplicate keys、格式錯誤、不支援 expansion 在 mutation 前拒絕。
- comments/formatting 不視為雲端資料。

### 15.5 三方同步規則

| 自 B 起變化 | 動作 |
| --- | --- |
| L、R 均未改 | 不處理 |
| 只有 L 新增/修改 | 上傳該 key，核對後更新 B |
| 只有 R 新增/修改 | 先保護本機檔案，再更新 L，成功後更新 B |
| L、R 同 key 且新值相同 | 接受共同值並更新 B |
| L、R 改不同 keys | 建立 merged result，分別套用後更新 B |
| L、R 同 key 且值不同 | 不改任一側；建立 `secret_conflict` decision |
| L 刪除 key | pending deletion；建立 deletion decision，未確認不刪 R |
| R 刪除 key | 保留 L；建立 deletion/adoption decision，不自動重建 R 或刪 L |

若任何 conflict 存在，本次 reconciliation **MUST NOT** 對任何一側做部分 mutation，除非實作明確把互不相關 key 分成獨立 transaction 且能證明不會改變 conflict decision context；v1.2 預設採整次停止，簡化安全語意。

### 15.6 Secret decisions

Secret conflict/deletion 使用 Section 11 decision envelope。

例：同一 key 的 L/R 值不同時，UI 可提供：

- 保留 local 並更新 shared；
- 採用 shared 並更新 local；
- 兩邊都不修改。

Agent MUST 只顯示 key name 與 conflict source，不顯示值。

使用者選擇後：

1. 重新讀 B/L/R；
2. 驗證 decision 未 stale；
3. 再計算 mutation；
4. 執行並核對；
5. 成功後更新 B。

### 15.7 Baseline protection

B 可能包含敏感內容或等效可比對資料：

- 必須與 `.env` 同級保護；
- 永不進 Git、model prompt、一般 logs；
- 不把公開 hash 當成「非敏感」，避免低熵 secret 被猜測。

### 15.8 同步時機

Adapter 可在以下入口自動執行：

- resume；
- 需要 secrets 的 run/test 前；
- checkpoint/handoff 前；
- 使用者明確要求時。

沒有背景 daemon。「自動」不代表每次 editor save 都立即上傳。

### 15.9 Writer fencing

只有目前 authority 的有效 writer session 可以上傳 shared changes。

失效/fenced/offline session：

- 只能保存本機 `.env`；
- 不能上傳 shared config；
- 重新取得合法 ownership 後先 reconciliation，再決定上傳。

### 15.10 寫入與 timeout

- 先固定本機資料與保存本機檔案，再讀遠端並計算 diff。
- 只寫指定 key。
- 本機更新使用 private temp file + atomic replacement 或等效安全方法。
- 發現 reconciliation 期間 `.env` 被再次修改時停止 replacement，重新計算。
- Doppler 不假設 compare-and-swap 或整批 transaction。
- 實作前必須驗證 API batching/concurrency/error semantics。
- timeout/partial success 後逐 key 核對；未確認項目維持 unknown/pending。
- 不使用 rollback 去覆蓋可能屬於他人的新值。
- 不重新上傳舊 snapshot 來「恢復一致」。

2026-10-14 使用者確認採基本同步模式：Doppler 不以 provider lease、atomic CAS 或 terminal-status API 作為寫入前提。Runtime 在每次送出前檢查 Git ownership 與目前遠端值，單 key 同步寫入後讀回驗證；成功才更新 baseline。逾時保留 unknown，`resolve` 只依目前遠端／本機值確認，不重送。這是目前狀態的確認，不是跨 Git／Doppler 的 transaction 保證；同 key 併發沒有 atomic CAS。既有 private journal 與 secrets upload grant 保留。實作與待驗收項目見 [basic checkpoint](basic-secrets-checkpoint.md)。

### 15.11 網路失敗

- 有可用本機 `.env` 時可做適合的 local work，但 `secrets_shared=false`。
- 缺必要本機設定時停止相關命令。
- 已啟動 process 不會自動獲得新 env；由 Agent 在合適時機重啟。

### 15.12 Credential files

- Application 預設讀本機 `.env`；必要時由安全 launcher 注入特定 subprocess。
- 不將 application secrets 無差別注入整個 coding Agent process。
- 文字 PEM 可在明確 file mapping 下作 shared secret，adapter 寫 0600 file；`.env` 保存本機 path。
- binary `.pfx` / `.p12` 不做通用自動 sync；v1.2 僅報告 cross-machine not provided，除非另有明確 file adapter spec。
- certificate 是否可放 Git 依內容與 policy 判定，不只看 extension。

---

## 16. Code 與 Secrets 的 Handoff Boundary

Git code publication 與 Doppler secret sync 分開操作與回報。

正常停止順序：

1. 檢查並固定本機工作；
2. 嘗試已授權 secrets reconciliation；
3. 不論 secrets 是否全部成功，只要 code snapshot 可安全發布，即可進行 handoff；
4. handoff commit 原子發布 code/Plan/state/summary 並 release owner；
5. summary 只列需要的 key names 與 non-secret sync status，不含值。

Secret sync 失敗不必永久阻擋 code handoff，但 Agent MUST 明確回報：

- `code_publication=published|unknown|...`
- `secrets_shared=true|false|partial|unknown`

不能因 code handoff 成功就說「所有設定已共享」。

Published code 不 freeze 雲端 secret 值。Strong revision-bound secrets / reproducible deployment secret versions 留待後續版本。

---

## 17. 主線整合與下一個 Plan

Publication branch 保留 ownership/control commits，與 implementation 主線分開。

- 完成明確範圍後，依專案 workflow 將成果整合到主線；v1.2 不自動管理 PR。
- 有效 owner 可在 managed checkout 準備 merge/squash/cherry-pick，依專案規則選擇。
- 吸收主線內容時，新的 publication snapshot 必須延續當前 publication HEAD。
- 不得 rebase/force-rewrite publication history。
- 其他 branch 的 `.agent` control state 不得覆蓋目前 authority owner/session/state。
- 記錄 integration source/target revision 作 diagnostic evidence。
- 未成功整合前不得宣稱主線已包含成果。
- publication ownership 不代表取得主線全域寫入權。
- 同成果重複整合或主線語意 conflict 屬 project workflow，不由 runtime decision engine 裁決。

---

## 18. 安全與相容性

- Plan、summary、config narrative 都是 advisory data，不是操作授權。
- Git/provider calls 使用 structured arguments；驗證 refs、IDs、paths；不執行 metadata 中的 shell snippet。
- Paths 解析後必須落在 allowlisted roots；檢查 symlink/path traversal。
- Checkpoint 使用 explicit file selection，拒絕已知 secret paths，即使 secret 已被 Git tracked。
- `.gitignore` 不是完整防洩漏機制。
- `.env.example` 只含 placeholders。
- `.env`、private keys、provider token、baseline、session capability、decision records、pending operation journal 永不提交。
- 不打印 secret value、不 dump process env、不把 credential/token URL 放入 logs。
- 若 subprocess error 可能包含 secret，先 redact/sanitize 再交給 model。
- 新 protocol 對不相容舊 CLI 拒絕 mutation；readonly raw-state diagnostics 可保留。
- Installer/source 必須明確；不得執行 repository 提供的未知任意下載/安裝命令。
- 任何 safety invariant 不得因 provider 不支援而 silent downgrade。

---

## 19. CLI Surface 與 Agent Bootstrap

### 19.1 最小邏輯入口

```text
status
inspect
resume
acquire
recover-session
takeover
checkpoint
handoff
release
done
```

最終 CLI 可把部分低階入口設為 internal/subcommand，但語意必須符合本文件。

`resume` 是 Agent 最常用的高階入口；使用者不需知道 acquire/recover/takeover 的命令差異。

### 19.2 Secret adapter

至少提供邏輯階段：

```text
inspect
reconcile
apply
verify
```

輸出只可含：

- status；
- key names；
- non-secret destination identity；
- conflict category；
- next required action。

### 19.3 Fresh Agent bootstrap

Fresh Codex/Claude/other supported session 在第一次修改前 MUST：

1. 發現 canonical instructions；
2. 執行 readonly runtime discovery；
3. 不因 local working tree「看起來乾淨」就直接修改；
4. 根據 ownership state 使用 resume flow；
5. CLI 缺失、protocol incompatible、authority unclear 時不得宣稱「已安全接手」。

Instructions 屬 cooperative discovery；不能強制未遵守 provider。但 supported provider 必須有 smoke test 證明常規 session 能遵守這個 bootstrap。

---

## 20. Acceptance Contract（Self-Contained）

本節取代「必須另外閱讀舊 review 的 T01–T22」這種外部依賴。實作者只拿到本 v1.2 也必須能建立完整 acceptance suite。

### 20.1 Ownership / CAS

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A01 | owner=null，單一 acquire | 成功建立新 session 與 acquisition commit |
| A02 | 兩個 clone 對同一 HEAD 同時 acquire | 最多一個成功；另一個 CAS rejected |
| A03 | owner=other session 時直接 acquire | 不 mutation；進入 takeover decision path |
| A04 | valid local session record + remote owner same session | recover-session 成功，不建立新 owner identity |
| A05 | remote owner same ID 但 local capability 遺失 | 不允許 self-recover；視為不可證明 owner |
| A06 | valid takeover decision + unchanged HEAD/owner | new session CAS takeover 成功 |
| A07 | takeover 後舊 session checkpoint | 必須 rejected/fenced |
| A08 | takeover 後舊 session release | 不得清除新 owner |
| A09 | owner 自己 release | control-only commit 清 owner，不改 Plan completion |
| A10 | concurrent mutation 改變 HEAD | 舊 operation 不得只換 expected OID 自動重送 |

### 20.2 Handoff / Done / Snapshot

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A11 | handoff | 同一 commit 包含固定 snapshot + summary/state + owner=null |
| A12 | checkpoint | 保留 owner，發布完整可恢復 snapshot |
| A13 | done | Plan DONE + cursor null + state idle 原子成立 |
| A14 | done without explicit release | owner 行為依明確 intent，不自行猜測 |
| A15 | snapshot 建立後 editor 新增修改 | 新修改留本機，不誤標為此次已發布 |

### 20.3 Crash / Unknown Outcomes

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A16 | push 成功但 client 未收到 reply | 重啟後以 operation ID/commit evidence 證明 published，不重複操作 |
| A17 | push timeout 且遠端仍 expected HEAD | 依 backend contract 安全 retry 或新 operation；不得 blind duplicate |
| A18 | push timeout 且遠端變更 | publication_unknown/re-evaluate，不覆蓋他人變更 |
| A19 | acquire publish outcome unknown | 在 ownership outcome 未證明前不得進一步 writer mutation |
| A20 | release outcome unknown | 不宣稱 owner 已清除 |

### 20.4 Publication Integrity

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A21 | publication branch 被刪除 | `publication_integrity_violation`；不得自動 recreate |
| A22 | publication HEAD 被 force-rewrite 到 non-descendant | violation；停止 mutation，保留 local work |
| A23 | owner/protocol metadata 被手改成 invalid | violation/validation failure；不視為合法授權 |
| A24 | 使用者對 violation 做決策後 HEAD 又改變 | 舊 decision stale；必須重新決策 |

### 20.5 Local Preservation / Concurrency

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A25 | dirty tracked/index | resume 前保存，不 reset/clean |
| A26 | untracked binary/ignored files | 不自動刪除或上傳 |
| A27 | local-only commits | 原 checkout 保留；managed checkout 不吞掉它們 |
| A28 | merge/rebase in progress | 不破壞現況；需安全隔離或停止 destructive action |
| A29 | 同 session 兩個 checkout 嘗試 mutation | repo/session local lock 只允許一個 write-capable checkout |
| A30 | 外部 editor 在 command lock 外改檔 | runtime 不假設 lock 能阻止；snapshot consistency 規則仍成立 |

### 20.6 Decision Boundary

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A31 | takeover required | 提供 natural-language options；user 選後 Agent 自行執行 |
| A32 | test file 可能有 bug | 不進 runtime decision mechanism；交由 coding workflow |
| A33 | 是否新增產品功能 | 不進 runtime decision mechanism |
| A34 | stale takeover decision | authority 改變後舊同意失效 |
| A35 | Agent 無法代理 OAuth/2FA | 可要求使用者完成該 auth step，但不要求使用者操作 agentctl/Git recovery |

### 20.7 Plan / Workflow

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A36 | working state 指向不存在 Plan | validation fail，禁止 mutation |
| A37 | current_step 不存在 | validation fail |
| A38 | Plan step reorder/insert | stable Step IDs 不重編號 |
| A39 | owner release while Plan working | Plan 仍可 working，等待下一 session |
| A40 | checkpoint 有測試摘要但無 revision evidence | test status 必須 unknown/invalid，不得宣稱 passed |

### 20.8 Secrets Reconciliation

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A41 | 新機沒有 L，有可讀 R | 建立本機 secret material，建立 B |
| A42 | 有 L/R、無 B、值相同 | initial reconciliation 建 B |
| A43 | 有 L/R、無 B、同 key 不同值 | 不修改；decision |
| A44 | 只有 L 自 B 修改 | 上傳該 key，verify 後更新 B |
| A45 | 只有 R 自 B 修改 | 保護 L 後套用 remote，verify 後更新 B |
| A46 | L/R 改不同 keys | 安全 merge，成功後更新 B |
| A47 | L/R 同 key 不同值 | 不顯示值；decision；未決前不 mutation |
| A48 | L 刪 key | 未明確 deletion decision 前不刪 R |
| A49 | R 刪 key | 保留 L，未決前不自動重建 R/刪 L |
| A50 | secret decision 後 B/L/R 改變 | decision stale，重新 reconcile |
| A51 | fenced old session 嘗試 secret upload | 拒絕 |
| A52 | secret update timeout/partial success | per-key verify；未知項目保留 unknown/pending |
| A53 | network fail 但 local secrets 可用 | 可 local work，`secrets_shared=false` |
| A54 | log/error output | 不得出現 secret value |

### 20.9 Security / Host / Bootstrap

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A55 | `.env` 已被誤 tracked | checkpoint 必須拒絕 secret publication |
| A56 | secret file mapping 有 path traversal/symlink escape | 拒絕 |
| A57 | installer 發現既有 VPN/firewall/SSH/config | inspect/preserve；不得任意停用或覆寫 |
| A58 | 必要 host 修改具破壞性 | 先 backup/recovery handle + minimum change |
| A59 | fresh supported Agent session | 修改前能發現 instructions 並走 readonly discovery/resume |
| A60 | protocol version 不相容 | mutation fail closed；可 readonly diagnostics |

### 20.10 Code + Secrets Partial Handoff

| ID | 驗收情境 | 必須結果 |
| --- | --- | --- |
| A61 | code handoff 成功、secret sync 失敗 | 可 release owner，但回報 `secrets_shared=false/partial/unknown` |
| A62 | secret sync 成功、Git handoff unknown | 不宣稱完整 handoff 成功 |
| A63 | 下一 Agent 缺必要 secret | 停止相關 runtime/test，不能猜值 |

---

## 21. 實作順序

### Phase 1 — Git core

- authority binding；
- single publication ref；
- acquire/recover-session/takeover/release；
- repo/session locks；
- fenced CAS publication。

先通過 A01–A10、A29。

### Phase 2 — Crash / integrity / preservation

- operation journal；
- unknown outcome recovery；
- integrity violation；
- preserve-first managed checkout。

通過 A16–A30。

### Phase 3 — Plan / handoff workflow

- checkpoint/handoff/done；
- Plan/Step invariants；
- mainline integration guards。

通過 A11–A15、A36–A40。

### Phase 4 — Decision engine

- decision envelope；
- stale invalidation；
- natural-language user options；
- strict control-plane scope。

通過 A31–A35。

### Phase 5 — Local-env / Doppler adapter

- manifest；
- parser；
- baseline；
- B/L/R reconciliation；
- secret decisions；
- timeout verification。

通過 A41–A54、A61–A63。

### Phase 6 — Security / UX / provider smoke tests

- host preservation；
- secret leak guards；
- bootstrap discovery；
- Ubuntu + WSL validation；
- selected Git hosting backend；
- isolated Doppler test project/config。

通過 A55–A60。

### 21.1 測試環境

先使用：

- WSL/Ubuntu；
- local bare Git remote；
- 至少兩個獨立 clones；
- 多個 local process/session；
- 隔離 Doppler test project/config；
- 非正式 credentials。

之後再驗證實際 Git hosting backend 的：

- protected branch policy；
- conditional update behavior；
- auth/read/write permissions；
- ref deletion/rewrite visibility。

未驗證的平台/provider 不列為 supported。

---

## 22. 尚待 Prototype / Backend 驗證的事項

以下不是留給實作者自行猜測的 safety semantics，而是需要以 prototype 決定最終 implementation detail：

- hosting backend conditional ref update 與 protected branch 相容性；
- Doppler API/CLI 更新粒度、batching、timeout reconciliation、concurrency semantics；
- dotenv round-trip、多行值與 file mapping 的精確 parser contract；
- runtime language、packaging、installer；
- machine-readable JSON schemas 的最終欄位名稱；
- non-Ubuntu platforms；
- additional coding providers；
- large files/submodules；
- binary credential adapter。

若 backend 不支援本文件的 safety contract：

- MUST 明確標記 unsupported；或
- 修訂 protocol version 與 design spec；
- MUST NOT silent downgrade 成較弱語意卻仍宣稱符合 v1.2。

---

## 23. v1.1 → v1.2 主要變更

| 類別 | v1.2 closure |
| --- | --- |
| Ownership vocabulary | 移除模糊 `recover-ownership`；拆成 acquire / recover-session / takeover / release |
| Session proof | 新增 Git 外 local session capability contract |
| Multi-checkout | 新增 repo/session-scoped local write lock |
| Takeover | 綁定 observed HEAD + owner 的 decision + CAS 新 session |
| Decision scope | 嚴格限制在 handoff/control-plane；禁止擴張到 project semantics |
| Decision safety | 新增 decision envelope 與 stale invalidation |
| Publication rewrite/delete | 正式定義 `publication_integrity_violation`，禁止自動 recreate/force restore |
| User UX | User 選意圖/option；Agent 自行操作 agentctl/Git/Doppler |
| Host safety | 新增 inspect-first / preserve-existing / minimum-change guardrail，涵蓋 VPN/SSH/firewall/provider config |
| Secrets conflict | 統一使用 decision protocol，選擇後重新讀 B/L/R 再 apply |
| Acceptance tests | 內嵌 self-contained A01–A63，不再依賴外部 T01–T22 才能實作 |
| Implementation status | 文件可作 implementation baseline；runtime 本身仍需 prototype/test 後才算 verified |

---

## 24. Traceability 與 References

本版吸收並取代以下文件作為實作基準：

- `agent-handoff-runtime-solution-design-v1.0-draft.md`
- `agent-handoff-runtime-solution-design-v1.0-review.md`
- `agent-handoff-runtime-solution-design-v1.1.md`

External references retained for implementation verification：

- Git publication: https://git-scm.com/docs/git-push
- Doppler CLI: https://docs.doppler.com/docs/cli
- Doppler projects: https://docs.doppler.com/docs/create-project

Provider 免費額度、價格與商業政策屬外部可變政策，不寫死在 runtime protocol contract。

---

## 25. Implementation Definition of Done

本專案後續採使用者明確批准的 Doppler 基本同步修訂：逐 key 寫入、目前值確認與不重播 resolve；provider 原子 CAS、跨 Git／Doppler fencing、durable terminal API 不再是交付前提。以下 conflict／timeout／permission 驗收仍保留，但應按此基本契約記錄實測與故障注入的界線，不宣稱交易級保證。

不得只因 CLI 能跑就宣稱 v1.2 完成。

v1.2 runtime 只有在以下全部成立時才可標記 implementation verified：

1. A01–A63 全部有 automated 或可重複 acceptance evidence；
2. 至少一個真實 Git hosting backend 驗證 conditional publication semantics；
3. Ubuntu/WSL smoke tests 通過；
4. Fresh supported Agent session 能在第一次 project mutation 前自動走正確 bootstrap；
5. secrets adapter 在隔離 Doppler config 完成 conflict/timeout/permission tests；
6. logs / model-visible output 經 secret-leak tests；
7. takeover 與 publication integrity decisions 經 stale-decision tests；
8. project semantic conflicts 不會誤入 runtime decision engine；
9. host installation/recovery tests 證明不會任意破壞既有 VPN/network/SSH/provider configuration；
10. 所有 unsupported provider/platform/backend 明確 fail closed 或標示 unsupported，沒有 silent safety downgrade。

完成以上條件後，才可把文件狀態由「implementation-ready design」升級為「verified implementation baseline」。
