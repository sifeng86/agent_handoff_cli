# 專案全面檢查紀錄 — 2026-10-15

檢查基準為其他 Agent 已提交的 `3187e6e`。本輪由 Primary 審查與驗收，一位 GPT-6-Luna 執行唯讀的 CLI／bootstrap／交接流程檢查。沿用使用者批准的基本 Doppler 同步契約。

## 發現與修正

| 問題 | 原本可能發生的情況 | 本輪處理 |
| --- | --- | --- |
| P2：host 結果與 exit code 不一致 | `host restore` 回傳 unknown，shell 卻收到 0；`host verify` 尚未就緒也可能被腳本當成成功 | 將 CLI 共用的 exit-code 計算移到所有正常 dispatch 路徑之後，unknown → 5、not_ready → 6；保留原本的格式與例外處理 |
| P2：多餘的 positional action 被忽略 | `release unexpected-action` 仍可走到 release；使用者輸入錯誤可能被當成有效操作 | 在 backend／runtime 執行前拒絕不接受 action 的命令，回傳參數錯誤 2 |
| P2：Agent 必讀指令與產品能力矛盾 | Canonical instructions 告訴 Agent「Doppler 上傳不支援」，實際基本上傳已完成 | 改成明確的逐 key 同步、授權、readback 與不重播 resolve；仍清楚說明沒有原子 CAS |
| P3：首次安裝教學缺少入口檔案步驟 | 只執行 init 後，Agent 找不到必須先讀的 `.agent/instructions.md` | 新增完整教學，展示從安裝資源複製入口、提交 base，再 init／resume 的正確流程 |

兩項 CLI 問題先以隔離 mock 重現，再加入回歸測試。針對性測試 `test_foundation` 與 `test_host_recovery_cli` 共 18 項通過。沒有因審查新增交易層、狀態系統或新的產品流程。

## 檢查範圍與判斷

| 範圍 | 查核內容 |
| --- | --- |
| CLI 與公開結果 | command/action dispatch、參數拒絕、exit codes、JSON result registry、錯誤輸出與敏感欄位拒絕 |
| Ownership／Git | 明確 authority/ref、expected-OID publication、持久 journal、timeout resolve、stale decisions、takeover 與 session／checkout 綁定 |
| Snapshot／Plan | 明確 include、原 source/index 保留、managed checkout、READY／DONE 與 state 引用規則 |
| 私有資料 | repository 外的 private root、owner-private files、descriptor-relative IO、symlink/hardlink 拒絕、dotenv 不執行展開 |
| Secrets | binding/grant、B/L/R reconciliation、逐 key 寫入前後確認、unknown／partial baseline 保留、resolve 不重播、PEM mapping |
| Host／authority recovery | backup-first、受限還原、固定 lock ordering、selection journal 與能力不搬移 |
| 可交付性 | wheel／sdist 安裝資源、原始碼與交付包一致性、實際安裝後 CLI lifecycle／復原／切換 |
| 驗收索引 | 六份 audit 的 A01–A63 共 63 項齊全，引用的測試函式存在；34 份 schema JSON 可解析 |
| 使用教學 | 從新建 local Git repo 開始，兩個獨立 session，checkpoint → handoff → done；直接執行文件內 Bash blocks |

這份檢查結合人工程式審查、可重現案例與自動測試。既有 GitHub、Doppler 與模型 API 紀錄保留為歷史實測；本輪不重新呼叫外部服務，也不把歷史通過紀錄當成本輪執行。

## 本輪驗證狀態

- 18 項針對性測試通過。
- 新版 wheel／sdist 各自在隔離 environment 安裝通過；lifecycle、host recovery、authority transition 與資源比對通過。
- 已分別用 wheel 與 sdist 安裝的 CLI 逐段跑過教學全部 7 個 Bash blocks；原 source/main 不變，最後 owner 為空、Plan 為 DONE、state 為 idle。
- 本輪完整 unittest **446／446 通過，耗時 144.070 秒**；使用者已明確批准含 loopback TCP 的本機測試，採標準 unittest discovery，只執行一次完整套件。
- 38 個 runtime 模組、25 個 scripts、88 個測試／fixture 檔案、34 份 schemas 與 8 個 template assets，均已核對與已安裝驗證的內容一致。最終 source archive 僅刷新文件。

## 已知能力邊界

基本 Doppler 模式沒有同 key 原子 CAS 或跨 Git／Doppler 交易；避免多個人同時修改同一 key。新 session 的 private binding／grant 不會經 Git 自動繼承。工具不自動把交接 branch merge 到 main。原生 Windows/macOS、SHA-256 repositories、submodules/LFS 與已停用的 legacy native-tool 模型驗收路徑仍不支援。

一般使用者只需用自然語言交代任務，見 [簡明使用者指南](user-guide.zh-TW.md)；命令與技術流程由 Agent 執行，見 [Agent／維護者操作手冊](agent-operations.zh-TW.md)。本輪 [JSON 紀錄](comprehensive-review-2026-10-15.json) 保存驗證範圍；正式 wheel／sdist 與最終雜湊位於 `dist/`、`dist/package-verification.json`，舊交付包已另存保留。

## 使用者分工修訂

依使用者確認，原詳細教學移為 Agent／維護者手冊，一般使用者指南改為從 GitHub 發現工具開始，分成全新專案與既有專案兩種自然語言導入情境。原 7 段驗證命令完全保留，更新路徑後已用已安裝的 CLI 再次跑通。這次只調整文件與驗證 helper 的檔案路徑，runtime 未改動，未重跑完整套件。

導入說明現使用單一輸入範本，hahaha 範例只替換三個欄位。使用者指南與 Agent 必讀指令的固定回報區塊已逐字比對一致，明訂已完成／待確認／未完成與成功證據要求。這是 Agent 必須遵守的回報規範，不是新增自然語言解析或驗證引擎；未進行模型 API 重測。更新後的模板已通過 wheel／sdist 安裝與資源驗證。
