# Doppler 基本同步

先在 WSL 執行 `doppler login`。使用既有 project／config；secret 值只在 Doppler 或本機 `.env` 編輯，不貼到對話或命令參數。

在已初始化並取得 ownership 的 managed checkout 操作。下列命令中的 `COMMON` 代表每次附上的既有 `--checkout`、`--authority`、`--publication-ref`、`--private-root`、`--context-file` 參數，不是 CLI 旗標。

1. `agentctl secrets configure COMMON --secrets-mode doppler --doppler-project PROJECT --doppler-config CONFIG --manifest-file MANIFEST --grant-upload`：提出綁定與上傳授權。
2. `agentctl decision apply COMMON --decision-id RETURNED_ID --option-id approve-secret-binding`：確認上述綁定。
3. `agentctl secrets reconcile COMMON`：比較上次同步、本機與遠端，取得 `plan_id`；衝突則依回傳的 decision 處理。
4. `agentctl secrets apply COMMON --plan-id RETURNED_PLAN_ID`：執行本次同步。
5. `agentctl secrets verify COMMON`：確認目前狀態。

本機改 A、遠端改 B 時會合併兩邊；同一 key 有衝突時先選擇處理方式。沒有 `--grant-upload` 的綁定不允許上傳。Manifest 僅列出 key 與分類，不包含值。

若回傳 `unknown`，保存 `secret_operation_id`，執行 `agentctl secrets resolve COMMON --operation-id RETURNED_OPERATION_ID`。它只查核目前狀態，不重送寫入。尚未收斂時保留舊 baseline，再建立新的 reconcile plan。

基本版採逐 key 同步。請避免多個人同時修改同一 key；讀取與寫入之間沒有 Doppler 原子 CAS。詳細故障處理見 [secret recovery](secret-recovery.md)。
