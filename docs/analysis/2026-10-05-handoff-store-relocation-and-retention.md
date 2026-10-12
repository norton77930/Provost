# Handoff 紀錄搬移與保留規則：設計規格

日期：2026-10-05
狀態：2026-10-05 使用者核准，§10 四項皆採建議（A、5/3/30 天、只在手動 `prune --apply` 時刪、唯讀後備觀察 2 週後另案移除）。2026-10-05 實作完成並安裝（9aa2882，runtime_hash 7d31da04；回滾複本 `native-pilot-v1.bak-20261005`）。

## 0. 實作結果與後續

- 驗證：完整測試 188 OK / 2 skipped；安裝版與 repo 版 hash 一致；實機：worktree 讀到主 checkout 舊位置的 checkpoint（legacy_binding）、從 worktree 寫入新位置、主 checkout 讀到同一筆、SessionStart compact 由 worktree 注入新位置 checkpoint。
- 審查：code-reviewer 與 architecture-reviewer 各 2 個 major，已於 c9e6b9d 修正；安裝後實機發現「安裝前啟動的 session 只有舊位置綁定」缺口，於 53abbca 修正；追加 code review（e0a9400..53abbca）無 blocker/major。
- 後續（未做）：record/snapshot 在驗證參數前就把舊綁定複製到新位置；binding-only 清理路徑未比對 workspace_id；compaction-only session 不會過期；horizon.json 無 checksum；POSIX rename-back 分裂；F1/B1 的少數測試缺口；約 2026-10-19 移除 `legacy.py`；個人 CLAUDE.md §10 的 worktree mismatch 句子待使用者確認後修改。
分類：困難 / R2（儲存位置與資料搬移）/ 證據：測試 + 審查 + 架構
基底：`wip/adaptive-handoff` @ 14091b8（runtime_hash a122fe09，已安裝）

## 1. 白話摘要

交接筆記（handoff record）目前存在「當下所在 repo 目錄」底下的 `.provost/handoffs/`。這造成兩個問題：

1. **分家**：session 中途進 worktree，之後的筆記寫進 worktree 目錄；對話壓縮後只在當下目錄找，找不到之前的筆記。worktree 被刪時，裡面的筆記一起消失。
2. **無限累積**：每次記錄都存一份新檔，舊檔從不刪除；有紀錄的 session 目錄也永遠不清。

本規格把筆記統一搬到 repo 外的 `~/.claude/provost/handoffs/<repo 識別>/`，同一個 repo 的主 checkout 與所有 worktree 共用一份；並加上兩條清理規則：每個 session 只留最近幾筆、久未使用的 session 可整個清掉。

## 2. 依據（已量測或實測）

- 2026-10-04 量測（23 個 session、171 次寫檔派工）：Computer Use session 585b6633 進 worktree 後第一次 compact 得到 `No checkpoint`，原本 14 筆紀錄在主目錄的 store。
- 2026-10-04 實測（Claude Code 2.1.289，EnterWorktree session）：
  - Write 工具寫主 checkout：被擋（"This session is isolated in the worktree"）。
  - Bash（python 寫檔、shell redirect、cd）寫主 checkout：未被擋，屬 guard 未涵蓋的路徑，不可依賴。
  - Write 與 Bash 寫 `~/.claude/`：允許。
  - Hook 從 worktree session 寫 `~/.claude/provost/cache`：允許（585b6633 的 PreCompact 快取檔可對應到 worktree key）。
  - `git -C <主 checkout>` 被擋（guard 的 git 重導向檢查）；runtime 在 python 內部呼叫 git，不受此檢查影響，但新設計本來就只需要在當下目錄執行 git。
- 2026-10-05 確認（git 2.42.0）：在 worktree 內執行 `git rev-parse --path-format=absolute --show-toplevel --git-common-dir`，一次回傳 worktree 路徑與 `D:/OSS/Provost/.git`，其上一層即主 checkout。
- 官方文件：Claude 建立的 worktree 在 session 結束時會被刪除（worktrees.md "Clean up worktrees"）。
- 現有用量：7 個 workspace store 合計約 1.2 MB（約 12 天、約 180 筆 record）。

## 3. 範圍與非目標

**範圍**
- 新的 store 位置與 repo 識別。
- 身分檢查改為 repo 層級。
- 舊 store 的過渡（唯讀後備）。
- 每 session 保留最近 N 筆（record 與 snapshot）。
- 久未使用 session 的清理（延伸現有 `prune`）。
- `doctor`、SessionStart 注入文字、`docs/native-handoff.md`、`skills/handoff/SKILL.md` 的對應更新。

**非目標**
- 不改 payload 欄位或 32 KiB 上限。
- 不改 `config.json` 的 schema（保留數值用程式常數與 CLI 參數，見 §6.4）。
- 不做 Stop hook 結果提醒（handoff 第 3 項，另案）。
- 不自動搬移或改寫舊紀錄的內容（見 §5）。
- 不 push、不發 PR；Provost 公開 repo 的移植另案。

## 4. 新的儲存位置與 repo 識別

### 4.1 位置

```
<store home>/v2/repos/<repo_key>/sessions/<sha256(session_id)>/
    binding.json
    current.json   records/<revision>.json
    latest.json    snapshots/<snapshot_id>.json
    horizon.json   （新增，見 §6.2）
    .lock
```

- `<store home>` 預設 `~/.claude/provost/handoffs`，可用環境變數 `PROVOST_HANDOFF_HOME` 覆寫（測試隔離必需）。
- 版本目錄用 `v2`，與舊的 `.provost/handoffs/v1` 區分。

### 4.2 repo 識別（repo_key）

- Git repo：用 `git rev-parse --path-format=absolute --show-toplevel --git-common-dir` **一次 spawn** 取得兩個值。common dir 的上一層即主 checkout 路徑；`repo_key = sha256(normcase(resolve(主 checkout 路徑)))`。主 checkout 與其所有 worktree 得到同一個 key。
- 非 Git 目錄：`repo_key = sha256(normcase(resolve(cwd)))`，行為等同現在。
- bare repo、submodule、common dir 解析失敗：回報明確錯誤碼，不猜。
- 這台機器每次 spawn 約 1.2 秒；現行 `workspace_root` 已有一次 `git rev-parse`，合併成同一次呼叫，不增加 spawn。

### 4.3 身分檢查

- `binding.json`、record、snapshot 內的 `workspace_id` 改存 `repo_key`，讀取時比對 `repo_key`。
- 另存 `worktree_path`（資訊用途，不參與比對），方便人看出筆記是在哪個 worktree 寫的。
- `--workspace` 參數仍接受任何路徑（主 checkout 或 worktree），內部一律換算成 repo_key。

### 4.4 安全檢查

- 現行 `storage_ready` 檢查 `.provost/handoffs` 不被 git 追蹤、且被 ignore。新位置在 repo 外，這兩項不再適用，移除。
- 保留並改以 `<store home>` 為根的 `guard`：拒絕 symlink 與 reparse point，路徑必須在 `<store home>` 之內。
- 新增：`<store home>` 位於 Git work tree 之內、而且**沒有被該 repo ignore** 時，回報 `STORE_INSIDE_REPO` 並拒絕寫入，避免筆記被意外 commit。（實作時修訂：原文為「位於任何 work tree 之內即拒絕」，但使用者的 `~/.claude` 本身是 git repo，`.gitignore` 以 `*` 忽略 `provost/`；以 `git check-ignore` 探測實際寫入的 `v2/repos/<16 字元>` 路徑，被忽略即可接受。）

## 5. 舊 store 的過渡

**建議做法（A）：切換 + 唯讀後備，不搬移。**

- 新紀錄一律寫新位置。
- `status`、`recovery_context`（compact / resume 注入）在新位置找不到該 session 的紀錄時，唯讀查看舊位置：當下 workspace 與其 repo 主 checkout 的 `.provost/handoffs/v1`。找到時注入內容並標示「來自舊位置，唯讀」。
- 第一次在新位置 `record` 時，`expected` 為 null；不把舊鏈接到新鏈（舊紀錄的 checksum 綁定舊 workspace_id，改寫會破壞其完整性）。
- `list` / `resume` 預設只看新位置；`list --legacy` 可列出舊位置的 snapshot，`resume` 可從舊 snapshot 收養（沿用現有 adopt 流程，標記 needs-verification）。
- 舊位置一律不寫、不刪。使用者確認不再需要後，可自行刪除各 repo 的 `.provost/handoffs/`。
- 唯讀後備保留到使用者決定移除（建議觀察 2 週後另案移除）。

**其他做法**（不建議，列出供選擇）
- B. 一次性搬移並重新封裝：改寫 workspace_id 與 checksum，會失去原始紀錄的可驗證性。
- C. 只切換、不後備：切換當下正在進行的 session，compact 後會暫時看不到舊筆記。

## 6. 保留規則

### 6.1 每個 session 只留最近 N 筆

- `record` 寫入成功後，同一把鎖內，若 `records/` 超過 `KEEP_RECORDS`（預設 5），刪除最舊的多餘筆數。
- `snapshot` 同理，`KEEP_SNAPSHOTS` 預設 3。
- 刪除前先更新 `horizon.json`（§6.2），再刪檔；中途中斷時，下次讀取仍能正確判斷。

### 6.2 版本鏈與 horizon

現行 `_head` 要求鏈完整：任何 parent 缺檔即 `INCOMPLETE_CHAIN`。直接刪舊檔會被誤判成損毀。

- 新增 `horizon.json`：`{"records": "<已刪除區段中最新一筆的 revision>", "snapshots": "<同上>"}`。
- 讀取規則：鏈上**只有最舊那一筆**的 parent 可以缺檔，而且必須等於 horizon 記載的值；其他任何缺檔仍判 `INCOMPLETE_CHAIN`。
- horizon 檔本身損毀或與實際檔案不一致時，判 `CORRUPT_RECORD`，不靜默接受。

### 6.3 久未使用的 session

- 延伸現有 `prune`：除了「只有 binding.json」的目錄，也列出「最近活動早於 `--expired-older-than-days`（預設 30）」的 session 目錄。最近活動取 record / snapshot 時間戳與 binding、compaction、current、latest、horizon 檔 mtime 的最大值。`--older-than-days`（預設 14）維持只管 binding-only 目錄。（實作時修訂：原本兩者共用一個參數，審查指出舊習慣的小數值會誤刪有紀錄的 session。）
- 預設只列出（dry-run），加 `--apply` 才刪；`--session <目前 id>` 與其 `.pruning-` 殘留永遠不刪；`workspace_id` 屬於其他 repo 的目錄列為 `FOREIGN_REPO` 跳過。
- 目錄的鎖正被持有時跳過，列入 `skipped`。
- 沿用現有 prune 的兩階段刪除（先改名成 `.pruning-<digest>` 再複查、再刪）。
- `doctor` 新增 `expired_sessions` 計數；不自動刪除。

### 6.4 數值放哪裡

- `KEEP_RECORDS = 5`、`KEEP_SNAPSHOTS = 3`、預設 `--expired-older-than-days 30`、`--older-than-days 14`，放程式常數與 CLI 參數。
- 不加進 `config.json`：該檔的 key 是白名單，加 key 等於改 config schema，另案再議。

## 7. 對外介面的變動

| 介面 | 變動 |
|---|---|
| `doctor` | 新增 `store_home`、`repo_key`、`expired_sessions`、`legacy_store_present` |
| `status` / `record` / `snapshot` | 位置改變；錯誤碼新增 `STORE_INSIDE_REPO`、`REPO_KEY_UNRESOLVED`、`GIT_UNSUPPORTED`（git < 2.31）、`REQUEST_ID_EXPIRED`；移除 `NOT_WORKSPACE_ROOT`、`TRACKED_HANDOFF`、`HANDOFF_NOT_IGNORED`；`status` 退回舊位置時加 `legacy`、`legacy_path`；binding / record 另存 `worktree_path`（schema_version 不變） |
| `list` | 新增 `--legacy` |
| `prune` | 涵蓋過期 session；新增 `--expired-older-than-days`；輸出新增 `expired`、`expired_removed`、`expired_older_than_days`；`skipped` 新增 `STORE_BUSY`、`UNEXPECTED_CONTENT`、`FOREIGN_REPO` |
| 模式判斷 | `workspace_roots` 列入主 checkout 時，其 worktree 也視為 adaptive（實作時依審查補上） |
| SessionStart 注入文字 | 「Do not change workspace identity mid-session」改為「worktree 與主 checkout 共用同一份紀錄」 |
| `docs/native-handoff.md`、`skills/handoff/SKILL.md` | 對應改寫 |
| 個人 `~/.claude/CLAUDE.md` §10 | 「Work in a different worktree than the supplied identity is a mismatch」需改寫；屬 repo 外檔案，實作完成後另請使用者確認 |

## 8. 必須成立的主張與驗證方式

| # | 主張 | 證據 |
|---|---|---|
| C1 | 主 checkout 與其 worktree 得到同一 repo_key；非 Git 目錄維持現行行為 | 單元測試（含真實 `git worktree add` 的暫存 repo） |
| C2 | 在 worktree 中記錄、compact 後於另一個 worktree 或主 checkout 讀得到同一條鏈 | 整合測試 |
| C3 | 舊位置唯讀後備：讀得到、標示來源、絕不寫入舊位置 | 測試（檔案系統前後比對） |
| C4 | 保留 N 筆後，鏈讀取正常；中段缺檔仍判損毀；horizon 不一致判損毀 | 測試 |
| C5 | prune 過期 session：預設不刪、`--apply` 才刪、目前 session 與持鎖目錄不刪 | 測試 |
| C6 | store home 位於 Git work tree 內時拒絕寫入 | 測試 |
| C7 | hook 路徑不增加 spawn | 計時與 spawn 計數測試（比照現有 wrapper 測試） |
| C8 | 現有測試全數通過（基準 118 OK / 2 skipped） | 完整測試 |
| C9 | 儲存邊界、身分模型、錯誤處理無回歸 | `code-reviewer`（R2 必要）＋ `architecture-reviewer`（儲存邊界屬架構邊界） |

最終驗證（只跑一次）：完整測試 + 安裝後 `doctor` 比對 runtime_hash + 在一個 EnterWorktree session 實際走一次 record → compact 注入。

## 9. 實作切片（核准後）

| 切片 | 內容 | 角色 | 依賴 |
|---|---|---|---|
| S1 | repo_key、store home、身分檢查、安全檢查（§4） | `implementer-high`（一致性與儲存邊界） | — |
| S2 | 舊位置唯讀後備與 `list --legacy`（§5） | `implementer-sonnet` | S1 |
| S3 | 每 session 保留 N 筆與 horizon（§6.1–6.2） | `implementer-high`（鏈的一致性） | S1 |
| S4 | prune 過期 session、doctor 欄位（§6.3） | `implementer-sonnet` | S1、S3 |
| S5 | 文件、SessionStart 文字、skill 文字（§7） | main 或 `implementer-sonnet` | S1–S4 |

S2 與 S3 都依賴 S1，彼此檔案大多重疊在 `store.py`，序列執行，不平行。完成後依序：`code-reviewer` + `architecture-reviewer` → 修正 → 最終驗證 → 安裝（先備份）→ 使用者確認後改 CLAUDE.md §10。

## 10. 待使用者決定

1. 過渡做法：A（建議）/ B / C。
2. 保留數值：`KEEP_RECORDS` 5、`KEEP_SNAPSHOTS` 3、過期 30 天，是否合適。
3. 過期 session 清理：只在手動 `prune --apply` 時刪（建議），或在 SessionStart 自動清。
4. 唯讀後備的保留期限：觀察 2 週後另案移除（建議），或永久保留。

## 11. 風險

- repo_key 依賴主 checkout 路徑：主 checkout 搬家後會得到新 key，舊紀錄需靠 `list --legacy` 類機制或手動處理。此情況少見，列為已知限制。
- 多個 worktree 的 session 共用同一 repo 目錄：鎖仍是 per-session，不同 session 不互相阻塞；`list` 會看到同 repo 所有 session，`resume` 需選對 work_id（現行規則已要求有多個候選時詢問）。
- 刪除舊紀錄是不可逆操作：只刪超過保留數的舊 record / snapshot，與手動 `--apply` 的過期 session；舊位置一律不刪。
