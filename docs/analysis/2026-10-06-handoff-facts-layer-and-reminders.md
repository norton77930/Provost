# Handoff 事實層、提醒節流與存檔分級：設計規格

日期：2026-10-06
狀態：2026-10-06 使用者核准，§11 三項皆採建議（`native-reminder` 併入 `native-facts` 並保留一版轉呼叫、事實沿用 30 天過期、`status` 預填 `template.sources`）；session 期間 commit 改以 `git log --since=<首次碰觸時間>` 查詢（§4.3）亦獲同意。
分類：困難 / R2（handoff store 新增資料種類、hook 輸入解析）/ 證據：測試 + 實機 + 審查
基底：`wip/adaptive-handoff` @ 474799b（runtime_hash 7d31da04，已安裝）；工作分支 `handoff-facts-layer`

## 1. 白話摘要

handoff 目前只靠主 AI 自己呼叫 `record`。AI 沒記錄時，壓縮前的 PreCompact 找不到任何 checkpoint，只能回報 `NO_CHECKPOINT`，壓縮後的 AI 什麼都拿不到。

本規格把交接資訊拆成兩層：

1. **事實層**：系統自動記錄，不需要 AI 配合，也不呼叫 LLM。內容是改了哪些檔案、在哪個 repo 和分支、哪些還沒 commit、這段期間新增了哪些 commit、派過哪些子代理。
2. **判斷層**：沿用現有的 `record`，由 AI 寫決策、原因、阻塞和下一步。提醒改成「在真正的決策點提醒，而且每輪最多一次」。

壓縮時依兩層的狀況，把存檔結果分成「完整、部分、失敗」三級。「部分」時，請壓縮後的 AI 先根據摘要補一筆決策紀錄。開場訊息改用固定的優先順序和長度預算，超過時整筆刪除，不再整段報錯。

## 2. 依據（已量測）

- **專案 A session fe1b4e17（2026-10-05 到 10-06，約 21 小時）**
  - 三次壓縮都得到 `NO_CHECKPOINT`，transcript 裡對 runtime 的呼叫次數是 0。
  - 只派了 Explore 和 code-reviewer，程式由主 session 自己改，所以 `native-reminder` 一次都沒觸發。
  - session 結束時，worktree 和主 checkout 都還有未 commit 的修改。
- **65 個 hook 生效的 session（開頭含 `Native handoff runtime active`，涵蓋 10 個專案）**
  - 改檔方式（關鍵字估計，偏高）：Edit/Write 工具約 3,870 次；指令改檔約 1,500 次，其中 Python 腳本約 610、`sed -i`/`perl -pi` 約 300、heredoc/redirect 約 250、`cp`/`mv`/`rm` 約 150、`git checkout --` 等約 80、格式化工具約 60。約四分之一的改檔 hook 看不到。
  - 42 個 session 在對話中途 commit，合計約 340 次。commit 之後 `git status` 會變乾淨。
  - 有跨 repo 寫入的實例：Chrome Extension 的 session 用 `cp` 和 Python 寫進 專案 B。
  - `native-reminder` 在 2026-09-29 加入。之後共提醒 275 次，其中 225 次（82%）在同一輪就有 `record`。
  - 42 個主 session 自己改過 repo 檔案的 session 中，有 18 個一筆紀錄都沒有，分布在 專案 C、專案 B、Chrome Extension、Computer Use、Provost 和 專案 A。
  - 單一 session 最多提醒 46 次；另有兩個 session 各 38 次。
  - 模擬「上次紀錄後第一次改 repo 檔案就提醒」的上限：有改檔的 session 中位數 4 次、最多 17 次（該 session 有 70 輪）。
  - 只有 8 個 session 實際壓縮過，共 12 次；其中 7 個有紀錄。
- **參考 claude-mem（thedotmack/claude-mem @ bbe11c7）**
  - 事件先寫本機 spool 再背景整理（`src/cli/spool-hook-event.ts`）。
  - SessionStart 輸出有 10,000 字元的硬預算，按固定順序整筆刪除；註解說明 Claude Code 會把超量輸出換成約 2KB 的替代內容（`src/services/context/ContextBudget.ts`）。
  - 不採用的部分：每次工具呼叫都叫 LLM、常駐 worker 加資料庫、儲存原始工具輸入輸出。
- **S0 實機探測（2026-10-06，Claude Code 2.1.291，Windows 11，Git Bash）**
  - 方法：在 job 暫存目錄用 `claude -p --model haiku --settings <探測設定>` 開獨立子 session，探測 hook 把收到的輸入寫成檔案；沒有修改使用者的 settings。
  - 子代理的工具事件會觸發 PostToolUse，輸入帶 `agent_id` 和 `agent_type`；主 session 的事件沒有這兩個欄位。
  - PostToolUse(Agent)：前景子代理在它結束時觸發一次；背景子代理（`run_in_background: true`）在啟動時就觸發。
  - `async: true` 的 hook 在 Windows 正常執行（刻意延遲 2 秒的 hook 仍寫出紀錄）。
  - 背景子代理結果回來時會觸發 UserPromptSubmit，`prompt` 以 `<task-notification>` 開頭；輸入欄位有 `prompt_id`。
  - SessionStart 的 `additionalContext`：8,000 和 9,500 字元完整保留；10,500 和約 15,600 字元被換成 `<persisted-output>` 與 2KB 預覽，AI 只看得到前 2KB。9,500 那次另一支 hook（native-event）也輸出了約 1,600 字元，合計超過 10,000 仍完整，所以上限是**每支 hook 各自計算**。
  - **Windows 上 bash 的啟動成本**：`bash -c exit` 中位數約 656 ms；現有 `native-reminder` 中位數約 570 ms。腳本內每個子程序（`$(...)`、`printf | tr`、`seq`、`date`）也要再花數百毫秒：第一版探測腳本因此跑了 13 到 19 秒，超過 20 秒 timeout 而沒有輸出，改成純內建寫法後降到 1 秒內。**`native-facts` 不可以使用任何子程序或命令替換。**

## 3. 範圍與非目標

**範圍**
- 事實記錄 hook，以及每個 session 的事實 spool。
- PreCompact 整合事實：git 狀態、session 期間的 commit、跨 repo 偵測。
- 提醒規則：新增觸發條件，加上每輪最多一次的節流。
- 壓縮存檔三級狀態與通知文字。
- SessionStart 開場訊息的長度預算與刪除順序。
- `status` 輸出附帶事實摘要；新增 `changes` 查詢指令。
- `prune` 一併清理過期的事實檔。
- 文件（`docs/native-handoff.md`）與測試。

**非目標**
- 用 Haiku 或其他 LLM 代寫判斷層（等事實層上線並觀察後再評估）。
- 「有紀錄卻顯示 previous handoff save failed」的問題（快取裡見到 `GIT_UNREADABLE`），另案處理。
- 移除 `legacy.py`（已排定約 2026-10-19 另案處理）。
- Claudex bridge 與 Provost plugin（非 native）路徑的行為。
- `record` payload 的結構不變（`validate_payload` 的欄位不動）。

## 4. 事實層

### 4.1 收集什麼

| 事件 | 記錄內容 | 不記錄 |
|---|---|---|
| PostToolUse `Edit`、`Write`、`NotebookEdit` | 檔案路徑、時間、是否由子代理執行（`agent_id`/`agent_type`） | 檔案內容、diff |
| PostToolUse `Agent` | `subagent_type`、`description`（截斷到 120 字元）、`run_in_background`、時間 | 給子代理的 prompt |
| PostToolUse `EnterWorktree`、`ExitWorktree` | worktree 路徑或名稱、時間 | |
| PostToolUse `Bash`、`PowerShell` | 只取 `cd <路徑>`、`git -C <路徑>`、`Set-Location <路徑>` 的目錄路徑，用來找出動過的 repo | 指令內容 |
| UserPromptSubmit | 不寫入 spool，只依旗標決定是否提醒（§6） | 提示內容 |

子代理的工具事件也收集，並標記 `agent_type`。在 Claude Code 中，子代理的工具事件同樣會觸發 PostToolUse，輸入帶有 `agent_id`；這一點列入 §9 S0 實測。

### 4.2 怎麼收集

- 新增 `hooks/native-facts`，只用 bash 內建功能，不啟動直譯器，寫法比照 `native-reminder`。
- 每個事件把一行 JSON 附加到 `~/.claude/provost/cache/facts/<session_id>.jsonl`。
  - 檔名只接受 `[A-Za-z0-9-]` 組成的 session_id，其他字元一律放棄記錄。
  - 路徑字串直接沿用 hook 輸入裡已經轉義過的 JSON 字串，不重新編碼。
- 以下任一情況都輸出 `{}` 並以 exit 0 結束，絕不阻擋工具：設定沒有 `"handoff": true`、解析失敗、寫入失敗。
- 註冊方式（S0 後修訂，見 §6）：
  - PostToolUse `Edit|Write|NotebookEdit|Agent|Bash|PowerShell|EnterWorktree|ExitWorktree` 全部設成 `async: true`，只做收集，AI 不等待。
  - `UserPromptSubmit` 同步，負責判斷並輸出提醒（§6）。每輪只多一次 bash 啟動（約 0.6 秒）。
- 腳本內不得使用任何子程序：不可用 `$(...)`、管線、`date`、`seq` 等外部指令。時間取 `printf -v ts '%(%s)T' -1`，建立旗標檔用 `: > <path>`。

### 4.3 PreCompact 整合

PreCompact 的 Python 端依序執行：

1. 讀取 spool，與既有的 `facts.json` 合併後寫回 session 目錄（`atomic_write`，加 checksum），再清空 spool。合併內容：
   - 依路徑去重，記錄每個檔案的次數和最後時間。
   - 子代理動作分開統計。
   - repo 外的檔案（job tmp、scratchpad、`memory/` 等）只保留數量。
2. 找出 repo：
   - 來源包括改過的檔案、`cwd`、進過的 worktree，以及指令裡取出的目錄。
   - 每個路徑往上找到含 `.git` 的目錄，依 git toplevel 去重。
   - 最多 5 個 repo；超過時保留最近動過的 5 個，其餘只記數量。
3. 對每個 repo 執行（每個指令限時 3 秒，整體上限 12 秒，低於 PreCompact 的 30 秒 timeout）：
   - `git rev-parse --abbrev-ref HEAD` 取得分支。
   - `git status --porcelain=v1` 取得未 commit 的修改，只存路徑和狀態碼，最多 50 筆。
   - `git log --since=<該 repo 第一次被碰到的時間> --format=%h %s` 取得 session 期間的 commit，最多 20 筆，標題截斷到 80 字元。
4. 任何一步失敗都只記錄錯誤碼（例如 `GIT_TIMEOUT`），繼續處理下一步。

**以 git 為準**：hook 紀錄代表「這個 session 動過什麼」，git 結果代表「現在的實際狀態」。兩者不一致時（例如 `git checkout --` 還原了檔案），摘要以 git 為準。

### 4.4 儲存

- 新增檔案 `<session_dir>/facts.json`，與 `binding.json`、`current.json` 同層。
- 格式：`{"schema_version": 1, "kind": "facts", "workspace_id", "session_id", "updated_at", "last_change_at", "files": [...], "outside_repo_count", "agents": [...], "worktrees": [...], "repos": [{"root", "branch", "first_touched_at", "dirty": [...], "commits": [...], "error"}], "checksum"}`。
- `last_change_at` 取以下幾個時間中最新的一個：最後一次改檔（hook 紀錄）、session 期間最新 commit 的時間、未 commit 檔案的實際修改時間（檔案 mtime；已刪除的檔案略過）。
  - 2026-10-06 修訂：原本寫「git 偵測到未 commit 修改的時間」，實作成「整理當下的時間」。結果只要 repo 有未 commit 的修改，每次壓縮都會被判成過時，就算 AI 剛寫完紀錄也一樣。改用 mtime 可以同時涵蓋用腳本改的檔案。
- record 與 snapshot 的格式不變；事實獨立存放。這是本規格唯一的儲存格式新增，因此屬於 R2。
- 大小上限 64 KB。超過時依序刪減：先刪 `files` 的舊項目，再刪 `agents` 的舊項目，最後刪 `dirty` 超過 50 筆的部分。

## 5. 存檔三級狀態

PreCompact 判斷：

| 狀態 | 條件 | PreCompact 通知 |
|---|---|---|
| 完整 `complete` | 有 AI record，而且 `recorded_at` 晚於或等於 `facts.last_change_at` | 不顯示 |
| 部分 `partial` | 沒有 AI record 但事實已存；或 record 早於 `last_change_at` | `Provost: facts saved; decision record missing.` 或 `Provost: facts saved; decision record is older than the latest change.` |
| 失敗 `failed` | 事實也沒存成 | 維持現有 `handoff not saved` 加錯誤碼 |

- 有 record 時，照現狀建立 snapshot。
- 沒有 record 時，不再拋出 `NO_CHECKPOINT`，也不建立空的 snapshot。
- 通知去重沿用 `notifications.notice`，狀態碼改存 `partial-missing`、`partial-stale` 或錯誤碼。

## 6. 提醒規則

2026-10-06 S0 後修訂：因 Windows 上每次啟動 bash 約 0.6 秒，提醒不在改檔當下發出，改在**下一輪開頭**（UserPromptSubmit）判斷。做法參考 claude-mem：工具事件的收集一律 async，只在每輪開頭與 AI 說話。使用者已同意。

**旗標**（收集 hook 以 `: > <path>` 建立，只用內建功能）：

- `<session_id>.pending-edit`：**主 session**（輸入不含 `agent_id`）用 Edit、Write 或 NotebookEdit 改了 repo 內的檔案。
  - repo 外的檔案不建立旗標。判斷方式是路徑含 `/.claude/jobs/`、`/tmp/`、`AppData/Local/Temp`、`/memory/` 等片段（含反斜線寫法）就視為 repo 外。
- `<session_id>.pending-dispatch`：派出寫程式角色的子代理。角色清單沿用 `PROVOST_REMINDER_ROLES` 與其預設值。
- `<session_id>.reminded`：已經提醒過。

旗標都放在 `~/.claude/provost/cache/facts/`。

**UserPromptSubmit 判斷**：有 `pending-edit` 或 `pending-dispatch`，而且沒有 `reminded` 時，輸出一次提醒並建立 `reminded`；其他情況輸出 `{}`。

**`record` 指令**（Python）成功後，刪除該 session 的三個旗標。

效果：

- 每輪最多提醒一次。提醒一次後，直到下一次 `record` 前都不再提醒，避免洪水。
- 上一輪結束前已經 record 的話，旗標已被清除，不提醒。
- record 之後又改檔，旗標重新建立，下一輪會再提醒。
- 背景子代理的結果通知也會觸發 UserPromptSubmit（S0 實測），所以結果回來時也能提醒。
- async 收集可能晚於下一次 UserPromptSubmit 才寫入旗標。最壞情況是晚一輪提醒，可以接受。

提醒文字：`Handoff: since the last record this session edited repo files or dispatched a writer subagent. Record the decisions behind them now (status, then record); facts are captured automatically.`

`native-reminder` 不再註冊（它是 PostToolUse(Agent) 的同步 hook，每次派工要多等約 0.6 秒）。腳本保留一個版本，只輸出 `{}`，避免舊設定報錯，下一版再移除。

## 7. SessionStart 開場訊息預算

- 總長度不超過 8,000 字元。
- 不再因為超過長度而拋出 `CONTEXT_TOO_LARGE`。
- 內容依下表的優先順序組裝。超過預算時，從最低優先開始整筆替換或刪除，不切半筆：

| 優先 | 內容 | 超過時 |
|---|---|---|
| 1 | 身份與操作說明（runtime、workspace、session、指令用法） | 保留；若單獨就超過，改用最短版本（只留路徑和 `status` 用法） |
| 2 | 存檔狀態與該做的事。`partial` 時加一句：先依壓縮摘要補一筆 record，事實已預填 | 保留 |
| 3 | 事實摘要：每個 repo 一行（分支、未 commit 數、session 期間 commit 數），加上子代理統計一行 | 保留；repo 超過 5 個時只列 5 個 |
| 4 | AI record 摘要（現有 `_payload_text`，上限 4,500） | 先縮成最近 3 個 decisions 的 digest，再縮成「完整內容在此檔案：<路徑>」 |
| 5 | 改過的檔案清單 | 最先刪，改成「完整清單：<facts.json 路徑>」 |

startup、clear、fork 不注入事實。只有 compact 和 resume 會注入，與現有的 `recovery_context` 一致。

## 8. 對外介面的變動

- `status`：輸出新增 `facts` 欄位，內容是 §7 第 3 項的摘要。`template.sources` 預填最多 10 個 repo 內改過的檔案（`kind: file`），AI 可以刪改。
- 新指令 `changes --workspace <root> --session <id>`：唯讀，輸出 `facts.json` 加上尚未整合的 spool 內容，並即時跑一次 §4.3 的 git 查詢。
- `prune`：一併處理 spool、`.state` 和 `facts.json`，過期條件與 session 相同。
- hook 註冊：需要在 `~/.claude/settings.json` 新增 `native-facts`（PostToolUse 一組 async、UserPromptSubmit 一組同步），並移除 `native-reminder` 的註冊。這一步屬於「寫入 workspace 外」，安裝時另行徵求同意；修改時只能用 parser 讀寫，因為該檔含有憑證。
- `runtime_hash` 的檔案清單新增 `hooks/native-facts`。

## 9. 實作切片（核准後）

| 切片 | 內容 | 驗證 |
|---|---|---|
| S0 | 實機探測（不改 runtime）：子代理的工具事件是否觸發 PostToolUse 並帶 `agent_id`；async hook 在 Windows 是否正常；背景子代理的結果通知是否觸發 UserPromptSubmit；SessionStart 實際上限 | 已完成，結果見 §2 |
| S1 | `native-facts` 收集與 spool（不含提醒） | bash 腳本測試：各事件類型、子代理標記、路徑抽取、異常輸入輸出 `{}` |
| S2 | PreCompact 整合、`facts.json`、git 查詢與逾時 | 用暫存 git repo 測：未 commit、session 期間 commit、checkout 還原、跨 repo、逾時 |
| S3 | 三級狀態與通知 | 測三種狀態的判斷與去重 |
| S4 | 提醒旗標與 UserPromptSubmit 判斷（`native-facts`）、`record` 清除旗標、`native-reminder` 改為只輸出 `{}` | 測提醒一次後不重複、record 後重新啟用、子代理改檔與 repo 外檔案不觸發 |
| S5 | SessionStart 預算與刪除順序、`status` 的 `facts` 欄位、`changes` 指令、`prune` 擴充 | 測各種超量組合都不拋錯、輸出都在 8,000 以內 |
| S6 | 文件、安裝（含回滾複本）、settings 註冊（徵求同意後） | 完整測試、hash 一致、實機壓縮一次確認 `partial` 和 `complete` |

S1 到 S5 依序進行，因為後面的切片依賴前面的資料格式。各切片交給 implementer 系列角色；S2 與 S4 涉及並行寫入與狀態一致性，交給 `implementer-high`。

## 10. 必須成立的主張與驗證方式

| 主張 | 證據 |
|---|---|
| 收集 hook 在任何異常下都不阻擋工具、不輸出錯誤 | S1 測試 |
| 不儲存指令內容、檔案內容、提示內容 | S1 測試以含密碼字串的輸入驗證 spool 不含該字串；code-reviewer |
| PreCompact 在 git 逾時或失敗時仍在 30 秒內完成 | S2 測試（模擬逾時） |
| 沒有 record 時壓縮結果為 `partial`，不再是 `NO_CHECKPOINT` | S3 測試、S6 實機 |
| 一輪最多一次提醒；提醒後到下一次 record 前不重複；record 後的下一次主 session 改檔會再提醒 | S4 測試 |
| AI 使用工具時不需要等待 handoff hook | S6 實機：設定中只有 UserPromptSubmit 與 SessionStart、PreCompact、PostCompact 是同步 |
| 開場訊息永遠不超過 8,000 字元且不拋錯 | S5 測試 |
| 安裝版與 repo 版一致，可回滾 | S6 hash 比對與回滾複本 |
| 新資料格式與既有 store、prune、legacy 路徑相容 | code-reviewer 與 architecture-reviewer（R2 必要） |

最終驗證（一次）：完整測試套件、hash 比對，加上一次實機壓縮。

## 11. 待使用者決定

1. **`native-reminder` 的處理**：建議併入 `native-facts`，舊檔保留一版作為轉呼叫入口，下一版移除。另一個做法是兩支腳本並存，但要協調節流狀態。
2. **事實的保留期**：建議與 session 的過期規則相同，沿用現有 prune 的 30 天。
3. **`status` 是否預填 `template.sources`**：建議預填，可以減輕 AI 的負擔；代價是 AI 可能原封不動照抄。

## 12. 風險

- **hook 輸入格式改變**：PostToolUse 的輸入欄位（`tool_input.file_path`、`agent_id`）若在 Claude Code 改版後變動，事實會缺漏。由 S0 探測與 S1 測試釘住目前的格式，並在缺漏時於事實摘要中標示「收集異常」。
- **Windows 上的 hook 成本**：每個 Bash 和 PowerShell 指令都會多啟動一次 bash。這部分設為 async，不會讓 AI 等待，但會增加背景負擔。S0 會量測單次耗時。
- **`git log --since` 依 commit 時間判斷**：rebase 或 amend 會改變時間，可能多列或少列 commit。摘要只是提示，不作為正式證據。
- **提醒仍需 AI 配合**：歷史資料顯示約 82% 會照做，但個別 session 差異很大（例如 12 次只照做 2 次）。事實層是保底，`partial` 狀態會在壓縮後再要求補記一次。
