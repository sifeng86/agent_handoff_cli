# Agent Handoff v2 — 需求與流程草案

- 狀態:草案,需求收集中(使用者尚有補充)
- 日期:2026-10-15
- 標記:**[確認]** = 使用者已表明的需求;**[提案]** = Claude 的設計建議,待使用者確認

## 1. 背景與問題

現行 v1.2 系統(ownership/CAS/fencing、managed checkout、34 個 schema、20 多個 CLI 指令)以「多 agent 並行搶寫入權」為假設設計,對「一個人依序換 agent/電腦交接」過於複雜,主要 UX 問題:

1. 沒有自動觸發機制:保存與交接完全依賴使用者記得說「今天先做到這裡」/「準備交接」;直接關視窗則什麼都沒存,寫入權仍被占用。
2. Agent 在 private root 下的 managed checkout 修改程式,使用者在 IDE 開的資料夾看不到。
3. 每個指令要帶 5 個參數,模型易出錯、耗 token。
4. 導入需貼固定範本、回報有 9 個固定欄位,儀式感太重。

## 2. 需求

### 2.1 整體

- R1 **[確認]** 目的只是交接,系統要比 v1.2 簡單得多。
- R2 **[確認]** 使用者不需要提醒 agent;agent 應主動處理接手、保存與交接。
- R3 **[確認]** 同時支援 Claude Code 與 Codex。
- R4 **[提案]** 使用者需要開口的時機只剩:交代任務、決定 merge 進 main、登入/2FA、agent 不該自行決定的例外(push 分叉、疑似秘密檔案)。

### 2.2 Git / GitHub

- R5 **[確認]** Git 與 GitHub 操作全部交給 agent。
- R6 **[確認]** Commit 是必要的。
- R7 **[確認]** 預設所有開發自動進 `develop` branch。
- R8 **[確認]** 只有完成時才 merge 進 `main`;`main` 之後會改名為 `master`。
- R9 **[待使用者決定]** Push 時機:Claude 建議「每次 commit 後立即 push」(見 3.3)。

### 2.3 使用者自行修改

- R10 **[確認]** 使用者自己修改檔案或新增檔案時,納入 git 的動作也要自動化。

## 3. 設計流程 [提案]

### 3.1 核心不變式

**Agent 每一輪結束時,工作區必須是乾淨的(已 commit)。** 因此任何時候出現未 commit 的變動,一定是使用者改的。

### 3.2 構成要素

| 要素 | 內容 |
| --- | --- |
| 狀態檔 | repo 內 `.agent-state.md`(<30 行:Task/Completed/Current/Remaining/Important decisions),隨 commit 推上 GitHub,跨機器、跨 provider 共用 |
| Agent hooks | 同一組腳本,分別註冊到 Claude Code(`.claude/settings.json`)與 Codex(`.codex/hooks.json` 或 `~/.codex/hooks.json`) |
| Git hooks | pre-commit secret scan(如 gitleaks),對 agent 與使用者手動 commit 都生效 |
| Repo 設定 | `.gitignore`(排除 `.env`、金鑰)、`.gitattributes`(`* text=auto eol=lf`) |
| GitHub 設定 | `main`、`develop` 禁止 force push 與刪除 |

### 3.3 Hook 流程

| 事件 | 動作 |
| --- | --- |
| `SessionStart` | 1. 確認在 `develop`(不在且工作區乾淨 → 切換;不乾淨 → 停下問使用者)<br>2. `fetch`;落後則 `pull --ff-only`;分叉則停下回報<br>3. 工作區不乾淨 → 視為使用者修改,單獨 commit(`chore: user edits (manual)`)並告知 agent 改了哪些檔<br>4. 將 `.agent-state.md` + `git status` + `git log` 摘要注入為 context(使用者不用說「接手」) |
| `UserPromptSubmit` | 比對上一輪 Stop 時記錄的工作區指紋;有差異 → 使用者在兩輪之間改了檔,先單獨 commit 並告知 agent,避免被舊內容覆蓋 |
| `Stop`(每輪結束) | 1. 本輪 agent 有改檔但未 commit → block,要求 commit(可用 `wip:`)並更新 `.agent-state.md`<br>2. 有未 push 的 commit → 腳本執行:fetch → 落後則 ff → secret scan 通過 → push;失敗才交給 agent 處理並告知使用者<br>3. 記錄工作區指紋<br>注意:只有本輪有改檔才檢查,避免無限 block |
| `PreCompact` | 提醒先更新 `.agent-state.md` |
| `SessionEnd` | 只做極快的保險動作(1–3 秒上限,模型不參與);主要保障靠每輪的 Stop |

### 3.4 Commit 與 Push 規則

- Agent 每完成合理段落就 commit,訊息為英文 Conventional Commits。
- **Push 建議:每次 commit 後立即 push 到 `develop`。** 理由:依序跨機器交接時,最大風險是「沒 push」導致下一位接不到;push 到 `develop` 的代價低;只在交接時 push 等於又依賴人記得。
- 永不 force push;分叉時停下回報,不自動 rebase/merge。
- 使用者修改與 agent 修改分開 commit,方便追溯。
- Merge 進 `main` 只在使用者說「完成了」時;可選 squash 以清理 `wip:` 歷史。
- Branch 名稱(`develop`、`main`/`master`)由設定值提供,改名只改一處。

### 3.5 新增檔案的自動處理

| 檔案類型 | 處理 |
| --- | --- |
| 已被 `.gitignore` 排除 | 不碰 |
| 疑似秘密(`.env*`、`*.pem`、`*.key`、`id_rsa` 等) | 不 commit,問使用者 |
| 單檔 ≥30MB | 不 commit,依 3.7 詢問使用者是否改用 rclone |
| 其他一般檔案 | 自動 commit |

### 3.6 秘密處理

需求:
- R11 **[確認]** 帳號密碼、`.env`、cert、含密碼的 `config.json` 等需要有處理方式。
- R12 **[確認(暫定)]** 使用 SOPS + age:秘密加密後隨 repo 版本控制,不依賴外部服務。
- R13 **[確認]** SOPS/age 的指令操作交給 agent;使用者平常只需編輯本機 `.env`。

**三條原則:**
1. 明文秘密不進 git:`.gitignore` + pre-commit secret scan,對人和 agent 都生效;只有加密檔(`*.enc`)進 git。
2. 秘密值不進模型看得到的輸出:值只在 `sops` 程式內流動,agent 只看到 key 名稱。
3. repo 內加密檔是真實來源;本機明文 `.env`、cert 都是解密出的副本(被 ignore)。

| 類型 | 進 git 的 | 本機副本 |
| --- | --- | --- |
| `.env` 環境變數 | `.env.enc`(值加密、key 名稱明文) | `.env`(ignored),或 `sops exec-env` 直接注入 |
| Cert / 私鑰(PEM、p12/pfx) | `*.enc`(sops binary mode) | 解密到 ignored 路徑,權限 0600 |
| 混有密碼的 `config.json` | 拆開:不含密碼的 config;密碼改從環境變數讀 | 同 `.env` |
| 人用的帳號密碼(網站登入等) | 不進 repo | 密碼管理器,使用者自管,agent 不經手 |

**分工:**

| 情境 | Agent 自動 | 使用者 |
| --- | --- | --- |
| 首次設定 | 安裝 `sops`、`age` 到 `~/.local/bin`;產生金鑰;建立 `.sops.yaml`;加密既有秘密並 commit | **備份私鑰到密碼管理器**(agent 只告知路徑,不印內容) |
| 修改秘密 | hook 偵測 `.env` 變動 → 只列 key 名稱 → 加密、commit、push | 直接編輯 `.env` |
| 接手/開工 | `git pull` → 解密 `.env.enc` 成本機 `.env` | 無 |
| 執行程式/測試 | `sops exec-env` 注入 | 無 |
| 新增機器 | 新機器產生金鑰、公鑰登記為「待核准」並 push;舊機器 SessionStart 發現後**詢問**;核准後 `sops updatekeys`、commit、push | **確認是否為自己的機器**(每台一次) |
| 撤銷機器 | 移除公鑰、重新加密、列出需輪替的秘密 | 到各服務更換密碼 |

**必須由使用者決定的原因:**
1. 核准新機器 = 授予解開全部秘密的能力;自動核准會讓任何能 push 的人或被誤導的 agent 植入金鑰。
2. 私鑰備份:agent 無法存取密碼管理器,且私鑰不可進入模型 context。

**風險:**
- 私鑰遺失則秘密無法復原 → 至少一份備份。
- 撤銷機器時 git 歷史中的舊加密檔仍可被該私鑰解開 → 秘密本身必須輪替。
- Agent 有能力解密或讀 `.env` → 用 Claude Code permissions deny 規則與 Codex `PreToolUse` hook 降低風險,無法完全防止。
- 私鑰(`~/.config/sops/age/keys.txt`)在 repo 外,secret scan 也需能偵測 `AGE-SECRET-KEY-` 字樣。

**替代方案(保留):** Doppler — 適合團隊共用、需要網頁管理與存取紀錄時。

**刻意不做(v1.2 有、v2 拿掉):** 三方 reconciliation、per-key CAS、journal。

### 3.7 參考檔案與大檔案

需求:
- R14 **[確認]** 不限定特定用途(report、截圖、資料、模擬結果等),一律視為「下一位 agent 可能需要參考的檔案」。
- R15 **[確認]** 是否上傳外部 case by case,由使用者決定。外部儲存為 Google Drive。
- R16 **[確認]** 預設以 GitHub(一般 git commit)為主,不動用 rclone。
- R17 **[確認]** 單一檔案 ≥30MB 時,agent 在 commit 前提醒並詢問使用者是否動用 rclone。資料夾總量不檢查。

**決策流程:**

| 條件 | 動作 |
| --- | --- |
| 單檔 <30MB | 照一般規則 commit 到 GitHub |
| 單檔 ≥30MB | **commit 前**停下,列出檔案/資料夾與大小,詢問使用者:(a) 上傳 Google Drive(rclone)並在 git 留索引;(b) 仍 commit 到 GitHub(僅限 <100MB);(c) 加入 `.gitignore` 只留本機;(d) 刪除/不處理 |

**為什麼必須在 commit 前判斷:** 檔案一旦 commit,就永久留在 git 歷史中;之後刪除或移走也不會縮小 repo,只能改寫歷史(需 force push,違反 3.4 規則)。

**GitHub 限制對照:** 單檔 >50MB 會收到警告,>100MB 直接拒絕 push;repo 建議保持在 1GB 以下。30MB 門檻低於 GitHub 警告線,保留緩衝。

**索引(無論存哪裡都要寫):** 下一位 agent 需要先「知道檔案存在、內容是什麼」。
- 參考檔案在 `INDEX.md`(或專案既有的文件位置)記錄:用途、內容摘要、結論/重點、位置。
- 選擇 rclone 的檔案,另在 git 內的 manifest(例如 `artifacts.json`)記錄:本機路徑、大小、sha256、Drive 遠端路徑、對應 commit。本機原始檔放在 ignored 的 `artifacts/` 目錄。

**選擇 rclone 時的 Google Drive 設定:**

| 項目 | 設定 | 理由 |
| --- | --- | --- |
| 工具 | rclone(WSL 內),不用 Windows Google Drive 桌面版掛載 | `/mnt/g` 跨檔案系統 I/O 慢;rclone 可由 agent 操作 |
| 授權範圍 | rclone scope `drive.file`(只能存取 rclone 自己建立的檔案) | token 外洩時不會暴露整個 Drive |
| OAuth client | 建議使用者自建 Google Cloud OAuth client ID(一次) | rclone 內建共用 client 有速率限制 |
| 遠端路徑 | `gdrive:agent-artifacts/<project>/` | 專案隔離 |
| 上傳指令 | 只用 `rclone copy`,禁止 `rclone sync` | `sync` 會刪除遠端多出的檔案 |
| 完整性 | manifest 記 sha256,下載後本機驗證 | Drive 只提供 md5 |
| rclone token | `~/.config/rclone/rclone.conf` 視為秘密;可存入 SOPS 讓新機器免重新授權 | 每台機器各自 OAuth 亦可 |
| 首次授權 | 使用者在瀏覽器完成一次(WSL 無瀏覽器時用 `rclone authorize` 在 Windows 端完成) | 唯一需要人的步驟 |

rclone 是第一次被選用時才設定,不預先安裝。

**自動化:**

| 時機 | 動作 |
| --- | --- |
| Commit 前(agent 自己或使用者新增的檔案) | 檢查單檔大小,≥30MB 走上方決策流程 |
| 新增參考檔案 | Agent 更新 `INDEX.md`;使用者手動放入的檔案,詢問用途後寫入 |
| `SessionStart` | 只注入 `INDEX.md` 的精簡摘要;不下載 Drive 上的檔案 |
| 需要 Drive 上的檔案時 | Agent 依 manifest 按需下載並驗證 sha256 |

**注意:**
- 反覆修改的二進位檔(例如每次重新產生的圖片),每個版本都會留在 git 歷史;即使單次不到 30MB,累積也會快速變大。Agent 發現同一二進位檔反覆 commit 時應提醒使用者。
- 截圖或 report 可能含秘密/個資;文字可掃描,圖片難以自動檢查,上傳前需提醒。

## 4. 已知限制與風險

1. Agent 執行中使用者同時修改同一檔案:hooks 只在兩輪之間執行,無法偵測 → 規範為「agent 執行時不要改它正在改的檔」。
2. Codex repo 層 hooks 曾有不觸發的回報([openai/codex#17532](https://github.com/openai/codex/issues/17532)),實作時需實測;必要時改放全域 `~/.codex/`。
3. Codex 新 hook 或修改過的 hook 需在 `/hooks` 審查信任(綁 hash),每次改腳本都要重新信任。
4. WSL 中找不到 `codex` 指令,可能安裝在 Windows 端;影響 hook 腳本的執行環境與寫法(Codex 支援 `command_windows`)。
5. 不使用 Codex hooks 時,行為退回只靠 `AGENTS.md` 指令,需使用者開口。

## 5. 範圍外 / 待決

- Doppler:秘密改用 SOPS + age 後不再需要;保留為團隊情境的替代方案。
- v1.2 既有程式碼的去留(重做或刪減):待需求收齊後決定。
- 使用者尚有補充的問題:待補。

## 6. 參考

- [Codex Hooks 官方文件](https://learn.chatgpt.com/docs/hooks)
- [openai/codex#17532](https://github.com/openai/codex/issues/17532)
