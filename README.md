# Agent Handoff Runtime

依據 [Implementation Specification](docs/spec.md) 與 [Design v1.2](docs/agent-handoff-runtime-solution-design-v1.2.md) 實作。已提供 local Git backend 的 init、acquire、recover-session、decision-based takeover、checkpoint、handoff、release、done、operation resolve 與隔離 checkout resume。Private state 與 source checkout 分開，publication 使用固定 expected OID 的 CAS 與 durable journal。使用者批准的基本同步版已完成驗收：444 項測試、隔離 Doppler 實測與 wheel／sdist 安裝通過。範圍見 [completion audit](docs/design-v1.2-completion-audit.md)。

## 環境設定

在 Windows 11 編輯專案，於 WSL Ubuntu 使用已驗證的 Python 3.12 與 Git 執行。所有 runtime paths、Git 與 credentials 使用 WSL 環境。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Runtime 無第三方 dependencies；build backend 固定為 setuptools 75.8.0。離線開發可用 `PYTHONPATH=src python3 -m agent_handoff.cli`，不需安裝 package。

Schema fixtures 使用 test extra 的 `jsonschema`；runtime 使用 stdlib 驗證 private records，拒絕未知版本、額外欄位、不合法 journal 終態及 acquisition intent 不符。JSON Schemas 定義結構；跨欄位 identity／history checks 由 runtime 執行。

Wheel 安裝包含 `share/agent-handoff/template` 與 `share/agent-handoff/schemas`，位於安裝環境的 data directory；source distribution 也包含 hidden `.agent` assets。

## 使用流程

Agent 必須明確取得 publication destination、完整 publication／mainline refs 與批准的 base OID。首次初始化建立 control records；既有 branch 必須通過 validation。已註冊 branch 若遺失，不會自動重建。使用者透過自然語言決策，由 Agent 執行 runtime 操作。

Agent 先唯讀檢查，再使用明確 private context 執行 resume。原 checkout 的 staged／unstaged／binary／ignored files 與 local commits 保留，runtime 另外建立精確 revision 的 managed checkout。有待整合 local edits 時回報 not_ready，由 Coding Agent 處理語意整合。後續命令使用回報的 managed checkout；同一 session 不會自動共用到其他 checkout／context。

```bash
agentctl inspect --authority /absolute/path/to/repository --publication-ref refs/heads/publication
PYTHONPATH=src:tests python3 -m unittest discover -s tests -q
```

結果為 JSON；`owner_present` 只描述 remote control record，不能當成 local capability。Git failures 不輸出 subprocess response 或敏感 argv。PrivateStore 逐層拒絕 symlinks，以 directory descriptors 進行 0600 records 的 fsync／rename；private directories 為 0700。Context 僅存 private file，CLI 使用 `--context-file` 明確選擇，不把內容放進 argv。

以下為 Agent／維護者命令參考；destination 與 base 由使用者明確指定，沒有預設 remote：

```bash
agentctl context create --checkout /path/to/source --private-root /path/outside/repos/runtime
agentctl init --checkout /path/to/source --authority /path/to/remote.git --publication-ref refs/heads/publication --mainline-ref refs/heads/main --base APPROVED_BASE_OID --private-root /path/outside/repos/runtime
agentctl resume --checkout /path/to/source --authority /path/to/remote.git --publication-ref refs/heads/publication --private-root /path/outside/repos/runtime --context-file /path/outside/repos/runtime/context.json
```

Snapshot commands 使用 repeated `--include` 指定檔案（刪除也必須明確指定）、`--summary`，以及需要時的 `--state-file`。`done` 必須提供 `--release true` 或 `--release false`。Plan 格式為 Markdown 開頭的 JSON metadata comment，範例見 `template/plans/`。Config／manifest 採 JSON-compatible YAML subset。

`decision apply --decision-id ID --option-id OPTION` 重讀 authority 並核對 observation；integrity recovery 可先 `decision propose-integrity`，只提供當下可安全執行的 adopt／abort。`operation resolve --operation-id ID` 核對精確 prepared commit，不建立第二筆等價 mutation。

同 checkout/context 的 authority selection 持久化於 Git 外；改 authority flags 不會自動切換。`authority propose` 使用來源 `--authority`／`--publication-ref` 及明確 `--target-authority`／`--target-publication-ref`；`authority apply` 使用相同參數與 `--decision-id ID --option-id switch|abort`。來源 write capability 必須先 release；兩端 pending operations 必須先 resolve。Review 綁兩端 HEAD/owner/private dependencies；切換不建立 writer 或搬移 secret grants。新 target namespace（含已存在遠端 ref）先獨立 `init` 建立 binding/trust，再 acquire。

若本機 transition journal 未決，可執行 `agentctl authority resolve --checkout /path/to/source --private-root /path/outside/repos/runtime --context-file /path/outside/repos/runtime/context.json`。此命令只驗證本機 commit point，不讀寫遠端或移轉授權。Crash/lifecycle 與 schema 的驗證範圍見 [transition contract](docs/authority-transition.md)。

Secrets CLI 已串接 scope binding／decisions、B/L/R reconciliation、private baseline、local-env IO、Doppler 基本逐 key 同步與 journaled apply／resolve。上傳須明確授權，寫入前核對目前值，寫入後 readback 才更新 baseline；回覆遺失時以 `resolve` 查核目前狀態，不自動重送。此基本模式不提供跨 Git／Doppler 的原子交易或同 key 並行 CAS。PEM file-backed reconciliation 保存 0600 material，`.env` 僅保存批准的相對路徑；拒絕 tracked mapping、symlink／hardlink、非 PEM 與 binary pfx／p12。Mapped material 不可發布至 Git，即使副檔名為 `.txt`。Dotenv 不接受 expansion、command substitution、duplicate keys 或未定義 escapes；local replacements 保留 comments／local-only keys，並拒絕可觀察到的 editor changes。Local-env private patch 支援 PEM material 與 dotenv path；中斷時保留 journal，詳見 [secret recovery](docs/secret-recovery.md)。

## 驗證範圍

Doppler 日常操作見 [基本同步步驟](docs/basic-doppler-usage.md)。

[A01–A63 traceability](docs/acceptance-matrix.md) 列出每項原始情境、必要結果、candidate evidence 與缺口。Test 名稱和數量不代表完整驗收通過。

目前明確支援／拒絕範圍、enforcement points 與已存在的負向測試見 [support capability matrix](docs/support-capability-matrix.md)；未測試組合不視為支援。

Automated tests 使用真實 local Git repositories、actual process-crash harness 與 test-only secret adapter；涵蓋 ownership、CAS、stale decisions、snapshot modes、Plan invariants、preservation、B/L/R、partial handoff 與 secrets CLI。Test adapter 的 fencing 不代表 Doppler backend 能力。這些不是 A01–A63 全部通過證明，也未替代 hosting／Doppler／fresh provider 實測。

已測試：Python 3.12 / Ubuntu WSL / local Git authority、GitHub 隔離 branch 的 competing CAS、真實 Doppler scoped read／A41／A42，以及 Codex GPT-6-Luna／Claude Haiku 5.5 的 fresh-session bootstrap。Evidence 見 [hosting](docs/hosting-cas-evidence.md)、[Doppler](docs/doppler-read-evidence.md)、[Codex](docs/provider-bootstrap-evidence.md) 與 [Claude](docs/claude-bootstrap-evidence.md)。尚未驗證完整 hosting／Doppler matrix、跨 provider takeover 與完整 startup／host matrix。SHA-256 repositories、Windows native、macOS、submodules 與 LFS 不支援。

Secret partial recovery 保留舊 baseline；Doppler 基本模式以目前遠端值確認結果，未收斂時使用 fresh reconciliation 修復，resolve 不重送。已 release 的原 caller 可用歷史私有 proof 查核，不恢復 writer capability。詳見 [recovery](docs/secret-recovery.md)。

基本版已完成實際 Doppler 上傳／合併／衝突／注入遺失回覆／認證拒絕與清理驗收，見 [實測紀錄](docs/doppler-basic-evidence.json)。完整 444 項測試通過；provider 交易級 fencing 不屬於使用者批准的基本同步契約。

Wheel／sdist 的隔離安裝與 CLI lifecycle 驗證：見 [package verification](docs/package-verification.md)。

Claude Haiku 5.5 的隔離 feature workflow 已完成限定驗收：僅使用三個 MCP tools，先讀 canonical instructions／inspect／取得 ownership，再修改 managed checkout 的 `app.py`；原始 checkout 與 mainline 保持不變。見 [feature evidence](docs/feature-workflow-evidence.json) 與 [425 項整合驗證](docs/feature-integration-verification.json)。驗收工具位於 source archive 的 `scripts/provider_mcp_feature_smoke.py`，不包含在 runtime wheel；`--run-model` 會執行一次付費 API turn，必須另行授權。Legacy native-tool profiles 維持停用；此驗收不包含真實 Doppler 寫入。

`inspect`／`status` 使用與 runtime 相同的 snapshot validator，驗證完整 control records、authority config 與 Plan references；只讀檢查不建立 persistent trusted-history baseline。

Host 設定只讀檢查與安裝前後比對：`agentctl host inspect`／`host verify`，見 [host preservation](docs/host-preservation.md)。不可讀設定及 symlink target 明確列為未驗證；不替代 Windows VPN／firewall／live service 的完整驗收。

既有安裝的限定修復使用 source checkout 的 `scripts/installer_repair.py`。先在 launcher 健康時執行下列 backup，保存回報的 opaque handle；修復只接受同一個 prefix 的 `bin/agentctl`，且唯一差異必須是 shebang 指向不存在的 interpreter。Prefix、checkout 與 private state 必須分開。

```bash
/absolute/prefix/bin/agentctl host backup --host-file /absolute/prefix/bin/agentctl --checkout /absolute/project --private-root /absolute/private-state
python3 scripts/installer_repair.py --prefix /absolute/prefix --known-good-handle SAVED_HANDLE --checkout /absolute/project --private-root /absolute/private-state
```

修復會先驗證並回報目前 broken launcher 的 backup，再替換單一 launcher。Body、owner、mode 不符就拒絕；結果若為 `unknown`，保留 operation ID 並使用 `host resolve` 查核，勿重送修復。這個 source script 尚未包含於目前交付 wheel。

Latest verification: [basic release](docs/basic-release-verification.json) — 444 tests passed, real isolated Doppler acceptance and wheel/sdist installations verified. Download the wheel/source archive from `dist/`; final hashes are in `dist/package-verification.json`.

Python packaging metadata `>=3.12` is an installation floor, not a tested-version promise. Python 3.12 on Ubuntu/WSL is verified; later Python minors remain unverified. Legacy unrestricted Codex/Claude provider smoke commands fail closed with `legacy_provider_profile_not_isolated`. Historical bootstrap evidence is retained. The exclusive-MCP auth diagnostic and the verified three-tool Claude feature workflow are the current reviewed paths.
