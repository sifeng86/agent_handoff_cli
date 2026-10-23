# Agent 與維護者操作手冊

本文件提供給執行設定、操作與驗證的 Agent 或維護者。一般使用者不需要學習下列命令；對話式用法見 [使用者指南](user-guide.zh-TW.md)。下列技術流程由 Agent 負責執行。

這個工具幫助不同 Agent 接續同一份工作：把選定的程式碼、目前 Plan／Step 和交接摘要保存到指定 Git branch，並記錄哪一個 session 可以繼續寫入。需要共用環境變數時，再選用 Doppler。

你可以先完成下方的本機練習；不需要 GitHub、Doppler 或模型 API 帳號。練習會用兩個 session，完整走過「開始工作 → 保存進度 → 交接 → 接手 → 完成」。

## 1. 先認識四個位置

| 名稱 | 用途 | 例子 |
| --- | --- | --- |
| Source checkout | 原本的專案，保留你的檔案與修改 | `~/projects/my-app` |
| Authority | 保存交接版本的 Git repository | 本機 bare repo 或明確的 GitHub SSH URL |
| Publication ref | 專用的交接 branch，必須寫完整 ref | `refs/heads/handoff/runtime` |
| Private root | 本機 session、復原紀錄與私有資料 | `~/agent-handoff-state/my-app/session-a` |

`resume` 會另外準備 **managed checkout**。收到它的路徑後，Agent 應在這個目錄工作，後續命令也使用這個目錄。原來的 source checkout 會保留下來。

Private root 必須放在所有相關 Git checkout 外面。不同 Agent／獨立 session 使用各自的 private root；同一 session 復原則沿用原路徑與 context file。`context.json` 是憑證：只把路徑交給 `agentctl`，不要開啟、貼到對話或提交到 Git。

## 2. 安裝

目前支援 Python 3.12、Git 與 WSL Ubuntu。先在 WSL 終端機確認：

```bash
python3 --version
git --version
```

在這份工具的原始碼目錄建立獨立 Python environment，安裝並啟用：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
agentctl --help
```

安裝時會取得固定版本的 build dependency，需要可用的套件來源。若已有交付 wheel，可改用 `.venv/bin/python -m pip install dist/agent_handoff-0.1.0-py3-none-any.whl`，不需要從原始碼建置。若 Ubuntu 缺少 venv 元件，先安裝對應的 `python3.12-venv`。

以下操作保持這個 environment 啟用。`agentctl --help` 是命令說明；版本可用 `python3 -c 'import importlib.metadata; print(importlib.metadata.version("agent-handoff"))'` 查詢。

## 3. 本機完整練習

每個程式碼區塊依序執行，使用同一個 Bash 終端機。這一節只會在新建的暫存目錄操作。`ah` 是為了少打重複參數的 Bash function，不是額外安裝的程式。

<!-- tutorial-demo:start -->

### 3.1 建立專案與交接 repository

```bash
LAB="$(mktemp -d "${TUTORIAL_PARENT:-/tmp}/agent-handoff-demo-XXXXXX")"
SOURCE="$LAB/source"
AUTHORITY="$LAB/remote.git"
PUBLICATION_REF="refs/heads/handoff/runtime"
PRIVATE_ROOT="$LAB/session-a"
CONTEXT_FILE="$PRIVATE_ROOT/context.json"
CHECKOUT="$SOURCE"

mkdir "$SOURCE"
git init --bare --quiet "$AUTHORITY"
git init --quiet --initial-branch=main "$SOURCE"
git -C "$SOURCE" config user.name "Handoff Demo"
git -C "$SOURCE" config user.email "handoff-demo@example.invalid"
```

先安裝 Agent 會讀取的專案指令。`init` 會建立 runtime 的 control records，**不會幫你複製這些入口指令**。這裡是全新的練習專案，所以可以直接複製。

```bash
TEMPLATE_DIR="$(python3 -c 'import sysconfig; from pathlib import Path; print(Path(sysconfig.get_path("data")) / "share/agent-handoff/template")')"
mkdir "$SOURCE/.agent"
cp "$TEMPLATE_DIR/AGENTS.md" "$SOURCE/AGENTS.md"
cp "$TEMPLATE_DIR/CLAUDE.md" "$SOURCE/CLAUDE.md"
cp "$TEMPLATE_DIR/.agent/instructions.md" "$SOURCE/.agent/instructions.md"
printf '.env\n.private/\n.venv/\n__pycache__/\n' > "$SOURCE/.gitignore"
printf 'print("Hello")\n' > "$SOURCE/app.py"
git -C "$SOURCE" add AGENTS.md CLAUDE.md .agent/instructions.md .gitignore app.py
git -C "$SOURCE" commit --quiet -m "Initialize handoff demo"
git -C "$SOURCE" push --quiet "$AUTHORITY" main
BASE="$(git -C "$SOURCE" rev-parse HEAD)"

ah() {
  agentctl "$@" --checkout "$CHECKOUT" \
    --authority "$AUTHORITY" --publication-ref "$PUBLICATION_REF" \
    --private-root "$PRIVATE_ROOT" --context-file "$CONTEXT_FILE"
}
```

實際專案如果已有 `AGENTS.md` 或 `CLAUDE.md`，應把 bootstrap 段落整合到既有指令，避免覆蓋原內容。先將 `.agent/instructions.md` 與入口檔案提交到準備作為 base 的 Git 版本。不要把 `template/.agent/` 整個覆蓋到既有專案。

### 3.2 初始化並取得可工作的 checkout

```bash
agentctl context create --checkout "$SOURCE" --private-root "$PRIVATE_ROOT"
ah init --mainline-ref refs/heads/main --base "$BASE"
ah inspect
ah resume > "$LAB/resume-a.json"
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); assert (r["status"], r["ownership"]) == ("ready", "owned"), "Resume is not ready"' "$LAB/resume-a.json"
CHECKOUT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["managed_checkout"])' "$LAB/resume-a.json")"
printf 'Working checkout: %s\n' "$CHECKOUT"
```

`inspect` 是唯讀檢查，放在 `resume` 前獨立執行。確認回傳 `status: ready`、`ownership: owned`，才開始工作。這裡的 Python 只讀取公開的 resume 結果，不讀取 context file。

如果回傳 `decision_required`，先處理第 6 節的選項；如果是 `not_ready`，先處理它指出的本機修改或缺少的環境設定，不要繼續貼後面的命令。

### 3.3 建立這次工作要完成的 Plan

Plan 是帶有固定 JSON metadata 的 Markdown 文件。這個練習的目標已確定：讓程式輸出 `Hello, handoff`，所以直接建立 READY Plan。

```bash
mkdir -p "$CHECKOUT/plans"
cat > "$CHECKOUT/plans/P-001-demo.md" <<'EOF'
<!-- agent-handoff-plan
{"schema_version":1,"id":"P-001","status":"READY","goal":"Print the handoff greeting.","scope":"Change app.py output.","non_goals":"No network or secret changes.","steps":["S-001"],"acceptance_criteria":"python3 app.py prints Hello, handoff."}
-->
# Greeting update

S-001: Update the greeting and run the program.
EOF

cat > "$LAB/working-state.json" <<'EOF'
{"schema_version":1,"status":"working","active_plan":"P-001","current_step":"S-001","blocker":null,"summary":""}
EOF

ah checkpoint --include plans/P-001-demo.md \
  --state-file "$LAB/working-state.json" --summary "Start greeting task"
```

`checkpoint` 保存明確列出的檔案與狀態，保留 ownership。沒有列入 `--include` 的程式檔案不會自動保存。你也可以先用 DRAFT Plan 討論需求，再於同意後改為 READY；範本的 DRAFT Plan 不能直接拿來 `done`。

### 3.4 修改程式並交接

```bash
printf 'print("Hello, handoff")\n' > "$CHECKOUT/app.py"
python3 "$CHECKOUT/app.py"
ah handoff --include app.py --summary "Greeting implemented; next agent verifies and closes Plan"
```

預期輸出 `Hello, handoff`，而 handoff 結果應為 `code_publication: published`、`ownership: released`。`handoff` 會保存選定檔案並釋放 ownership；這之後舊 session 不應繼續修改或 checkpoint。

這份練習沒有配置 secrets，所以 `secrets_shared` 通常是 `null`。有配置時，程式碼交接與秘密同步要分別看結果，見第 7 節。

### 3.5 用第二個 session 接手

模擬另一個 Agent：換一個 private root 與 context，並從原本的 source checkout 接手。新機器則先 clone 一份 source checkout，再使用自己的本機路徑。

```bash
CHECKOUT="$SOURCE"
PRIVATE_ROOT="$LAB/session-b"
CONTEXT_FILE="$PRIVATE_ROOT/context.json"
agentctl context create --checkout "$CHECKOUT" --private-root "$PRIVATE_ROOT"
ah init
ah inspect
ah resume > "$LAB/resume-b.json"
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); assert (r["status"], r["ownership"]) == ("ready", "owned"), "Resume is not ready"' "$LAB/resume-b.json"
CHECKOUT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["managed_checkout"])' "$LAB/resume-b.json")"
python3 "$CHECKOUT/app.py"
```

第二次 `init` 是在新的本機 private root 登記既有 publication，不會重建已存在的交接 branch。第二個 managed checkout 應已包含第一個 session 的程式與 Plan。

### 3.6 完成 Plan

```bash
ah done --release true --summary "Greeting verified and Plan completed"
ah inspect
git --git-dir="$AUTHORITY" show main:app.py
git --git-dir="$AUTHORITY" show "$PUBLICATION_REF:app.py"
```

`done` 會把已保存的 active READY Plan 改成 DONE、把 runtime 狀態設回 idle，這裡也選擇釋放 ownership。最後兩行分別應顯示原 main 的 `Hello` 與交接 branch 的 `Hello, handoff`。

<!-- tutorial-demo:end -->

練習完成後，`$LAB` 仍保留兩個 session 的資料，方便查看。這個工具保存交接 branch，不會自動替你 merge 到 main；測試完成、review 與合併程式碼仍使用專案原有流程。

## 4. 套用到自己的專案

先確認要使用的 repository、mainline ref、專用 publication ref 與 base commit。遠端可以是 `git@github.com:ACCOUNT/REPOSITORY.git`，不能填 `origin` 這類 alias；`--base` 使用經確認的完整 commit SHA，且遠端必須能取得它。

把練習中的變數換成自己的值：

```bash
SOURCE="$HOME/projects/my-app"
CHECKOUT="$SOURCE"
AUTHORITY="git@github.com:ACCOUNT/REPOSITORY.git"
PUBLICATION_REF="refs/heads/handoff/runtime"
PRIVATE_ROOT="$HOME/agent-handoff-state/my-app/session-a"
CONTEXT_FILE="$PRIVATE_ROOT/context.json"
```

接著依 3.2 初始化。第一次才提供 `--mainline-ref refs/heads/main --base APPROVED_COMMIT_SHA`；既有 branch 的新本機登記使用 `init` 即可。先在 WSL 完成 Git 認證；runtime 不會替你猜 credentials。

每次切換專案或 session，都重新確認這些變數。收到 `managed_checkout` 後更新 `CHECKOUT`；不要繼續把原 source checkout 當成目前的 write checkout。開新終端機也要恢復 `ah` function 與正確參數。

原 source 有未提交、staged、本機 commit 或 merge/rebase 中的工作時，`resume` 會保留原始資料，可能回傳 `not_ready`。由 Coding Agent 比較並整合到 managed checkout；不要用 reset／clean 清掉原本的工作，也不要反覆 resume 期待它自動合併。

## 5. 日常命令怎麼選

| 你要做的事 | 命令 | 結果 |
| --- | --- | --- |
| 看遠端目前狀態 | `ah inspect` 或 `ah status` | 唯讀，不取得 ownership |
| 開始／重新進入工作 | `ah resume` | 確認 ownership，準備 managed checkout |
| 同一 checkout／context 查回 ownership | `ah recover-session` | 查核既有憑證，不建立新 session |
| 保存一段進度 | `ah checkpoint --include app.py --summary "..."` | 發布選定檔案，保留 ownership |
| 交給下一位 Agent | `ah handoff --include app.py --summary "..."` | 發布選定檔案並釋放 ownership |
| 只釋放、不保存程式修改 | `ah release` | 未保存檔案仍留在本機 |
| 完成目前 Plan 並離開 | `ah done --release true --summary "..."` | Plan → DONE、state → idle、釋放 |
| 完成 Plan 但繼續持有 session | `ah done --release false --summary "..."` | Plan → DONE、保留 ownership |

多個檔案重複使用 `--include`；刪除檔案也要明確列出路徑。`done` 不接受自訂 `--state-file`，也不能同時 `--include` 它要完成的 active Plan。先用 checkpoint 保存 READY Plan 與 working state，再用 done 結束。

## 6. 看懂結果與處理中斷

大部分命令會輸出一個 JSON 結果。

| 欄位／狀態 | 簡單理解 | 下一步 |
| --- | --- | --- |
| `status: completed` | 這個操作完成 | 仍要看下面的個別結果 |
| `status: ready` + `ownership: owned` | resume 已準備好工作 | 使用回傳的 managed checkout |
| `decision_required` | 需要你選擇 | 檢視當次 decision 的 options，再套用選項 |
| `not_ready` | 缺少設定、整合或可用環境 | 依 next_action 處理 |
| `rejected`／`ownership: fenced` | 這次請求被拒絕或舊 owner 已失效 | 先查目前狀態 |
| `unknown`／`ownership_pending` | 還不能確認結果 | 保存 ID，使用 resolve 查核 |
| `code_publication: published` | 選定程式碼已發布 | 不代表 secrets 一定成功 |
| `secrets_shared: true` | 本次確認秘密已同步 | 不要把值貼到對話 |
| `secrets_shared: false/partial/unknown` | 尚未全部確認共享 | 依秘密同步結果另外處理 |

`owner_present: true` 只代表遠端有 owner，不代表目前 Agent 擁有憑證。Shell exit code：0 完成／ready、2 參數或資料格式、3 要求決策、4 拒絕、5 結果不確定、6 尚未就緒、7 integrity／public-output 驗證失敗、8 不支援。不要只用 exit code 0 判定整個 handoff 的每個部分都成功。

若另一個 session 尚持有 ownership，先與對方確認。需要接管時，使用當次回傳的 ID 和明確選擇：

```bash
ah decision apply --decision-id RETURNED_DECISION_ID --option-id takeover
ah resume
```

`abort` 可保留現狀。這些 ID 是需要替換的占位文字；不要重用舊 ID。如果 decision 已過期，重新檢查並選擇，不要自動批准新決策。

發生 timeout／中斷時，區分兩種 ID：

```bash
ah operation resolve --operation-id RETURNED_GIT_OPERATION_ID
ah secrets resolve --operation-id RETURNED_SECRET_OPERATION_ID
```

第一行查 Git 發布，第二行查秘密同步，依實際結果選擇使用。Resolve 可能仍回傳 unknown，這時保留私有紀錄與本機檔案，恢復連線或處理原因後再查核；不要刪 journal 或盲目重送原寫入。

## 7. 選用 Doppler

Doppler 是共用秘密值的後端。Git 交接可單獨使用，不需要先配置 Doppler。

### 7.1 宣告要同步哪些 key

先在 WSL 完成 `doppler login`，準備自己的 project／config。檔案雖然叫 `.yaml`，目前格式使用 JSON-compatible YAML subset，最簡單就是直接寫 JSON：

```json
{"schema_version":1,"keys":{"APP_TOKEN":{"classification":"shared","required":true},"LOCAL_ENDPOINT":{"classification":"local-only","required":false}}}
```

例如將它放到目前 managed checkout 的 `config/secrets-manifest.json`。Manifest 只放 key 名稱與分類，不放值。`shared` 參與 Doppler 同步；`local-only` 只留本機；`required` 表示執行依賴它的程式前必須可用。PEM 的 `file-backed` 進階用法見 [秘密復原說明](secret-recovery.md)。

用 `ah checkpoint --include config/secrets-manifest.json --summary "Declare secret scope"` 保存這份非秘密宣告，下一個 session 就能讀到。`--manifest-file` 本身不會自動發布檔案。若使用預設 `.agent/secrets.example.yaml`，應在最初的專案 base 就準備好；`.agent/` 是 control 區域，不能用一般 `--include` 更新。

### 7.2 綁定、批准、同步

在目前擁有 ownership 的 managed checkout，沿用 `ah` 的參數。這裡再用 `ash` 帶入剛才的 manifest，避免和專案可能存在的預設 manifest 混用：

```bash
ash() {
  ah "$@" --manifest-file "$CHECKOUT/config/secrets-manifest.json"
}
ash secrets configure --secrets-mode doppler \
  --doppler-project YOUR_PROJECT --doppler-config YOUR_CONFIG \
  --grant-upload
ash decision apply --decision-id RETURNED_DECISION_ID --option-id approve-secret-binding
ash secrets reconcile
ash secrets apply --plan-id RETURNED_PLAN_ID
ash secrets verify
```

每一步先看結果，再把該步回傳的 ID 填入下一步。`configure` 預期回傳 decision_required；`reconcile` 遇到衝突時也會回傳 decision，此時應選擇它實際提供的 `keep-local`、`use-remote` 或 `abort`，不是直接 apply 一個有衝突的 plan。未加 `--grant-upload` 的綁定不允許上傳。這組設定的秘密相關命令與自動同步交接都用 `ash`，例如 `ash handoff --include app.py --summary "..."`。

沒有本機值但遠端有值時，reconcile/apply 可下載；本機改 A、遠端改 B 時可合併；同一 key 兩邊改動則要求選擇。首次接手的新 session 必須自己批准 binding，舊 session 的 private grant 不會跟著 Git 傳過去。

秘密值由人員或受信任的本機程序寫入 Doppler／`.env`。不要在模型對話中提供值，不要讓 Agent 讀取 `.env`。本機 `.env` 必須是你擁有的 regular file，權限為 `0600`，例如 `chmod 600 "$CHECKOUT/.env"`；不要追蹤到 Git。支援範圍內不會執行 `$VAR`、`${...}`、`$(...)` 或反引號展開，這類值會被拒絕。

`secrets verify` 的 local availability 完整，才執行依賴那些值的程式或測試。若只有共享暫時失敗而本機 required keys 都可用，可繼續適合的本機工作；不要宣稱共享成功。

基本模式逐 key 寫入，會先讀目前值、寫後再讀回確認；結果不確定時用 `secrets resolve`，不自動重送。請避免多個人同時修改同一 key，這裡沒有原子 CAS 或 Git／Doppler 跨服務交易。

## 8. 最常見的卡點

| 情況 | 常見原因 | 處理 |
| --- | --- | --- |
| 找不到 `.agent/instructions.md` | 只執行 init，沒有安裝專案入口指令 | 依 3.1 在專案 base 準備並提交指令 |
| `Another checkout or process context...` | 使用原 checkout，或混用了其他 session 的 private root/context | 改用回傳的 managed checkout；核對同一 session 的路徑 |
| `Private state must be outside...` | private root 放進專案 | 使用 repository 外的獨立目錄 |
| `Unsafe ... permissions` | `.env` 或 private records 不是 owner-private | 檢查正確檔案的 owner／權限，不要用全目錄遞迴 chmod 猜修 |
| `Done requires a committed active READY Plan` | 沒有先保存 Plan／working state，或仍是 DRAFT | 依 3.3 checkpoint 後再 done |
| Git 認證／網路錯誤 | WSL credentials 或連線不可用 | 在 WSL 完成外部認證；已送出的操作先查 resolve |
| `recover-ownership` 不可用 | 使用舊命令 | 同 session 使用 recover-session；不同 session 走決策式 takeover |
| 某平台／repository 不支援 | 原生 Windows/macOS、SHA-256 repository、submodules 或 LFS | 使用已驗證的 Ubuntu/WSL 與 SHA-1 普通 Git repository |

遇到問題可分享公開 JSON 結果、命令名稱及工具版本。不要分享 `.env`、context file、private-root 內的原始檔案或未審查的 provider trace。

## 9. 給下一位 Agent 的交接提示

把下面內容填好交給新 Agent；使用者應先同意 repository、branch 和必要的接管決策。

```text
請先獨立讀取專案的 .agent/instructions.md，依它的流程接手。
Source checkout: /absolute/path/to/source
Authority: explicit repository path or SSH URL
Publication ref: refs/heads/handoff/runtime
Private root: a separate approved path for this session
如果已提供本 session 的 context file，僅將該路徑交給 agentctl，不讀取內容。
先 inspect，再 resume；確認 ready + owned，使用回傳的 managed checkout。
本次任務與完成條件：...
```

Codex／Claude 的專案入口檔都指向同一份 canonical instructions。工具本身不會自動呼叫模型；先前的模型驗收腳本是開發測試用途，日常使用不需要執行它們。
