# Provost 架構稽核與 Workflow 優化分析報告

- 稽核日期：2026-11-12
- 稽核對象：`D:\OSS\Provost`（本機 `main` = `125f7e2`；`origin/main` = `3f9e4ae`，即目前安裝的版本）與安裝後的 `~/.claude`
- 量測期間：2026-10-27 00:00Z 至 2026-11-12（約 16.5 天），97 個主 session 逐字稿檔、654 個 subagent 逐字稿檔
- 公開版本：本機機密衛生的細節（C6）已一般化，完整內容另交給維護者。
- 性質：**第一階段，唯讀**。未修改任何程式碼、設定、store 或正式環境；未執行 install / setup / prune --apply / migrate --apply / redact。

---

## 0. 摘要

1. **沒有 P0。** 安裝版與 `origin/main` 的 22 個 runtime 檔逐一比對相同；`install.py check` 回報 `drift: false`、35 個目標全 `ok`；`doctor` 正常；`main` 最近 6 個 commit 的 CI 全部成功。未發現會損毀紀錄或擋死 session 的缺陷。
2. **最大的可靠性問題是 hook 逾時後靜默失效（P1）。** Stop 驗收 gate 在期間內有 **89 次**跑滿 30 秒被 Claude Code 取消（10-27 那週 0 次、11-03 那週 32 次、11-09 之後 4 天 57 次），被取消時 gate 不做任何判定也沒有任何訊息。SessionStart hook 在主 session 有 **14 次**逾時，多數發生在 compaction 之後，正是需要注入 checkpoint 的時刻。
3. **最大的成本來源不是 Provost 注入的文字，而是主 session 每次請求的 context 大小。** 主 session 每次請求的 prompt 中位數 172K tokens，32.5% 的請求在 200K 以上；cache read 占主 session 估算成本 45%，1 小時 cache 寫入占 36%。Provost 的 policy（29.9K 字元）與 SessionStart 注入（中位數 7.2K 字元）相對很小。
4. **可避免的浪費集中在「儀式性請求」。** 主 session 19,877 個請求中有 3,012 個（15.2%）只做通知、handoff 紀錄、digest、驗收檔或 task 清單，估算 $196（主 session 的 13.2%）。其中只呼叫 `PushNotification` 的請求 1,656 個、只做 handoff 的 779 個。每個這類請求都要重讀約 165K tokens 的 cache。
5. **重複讀檔不是問題。** 同一 agent 內「檔案未變卻重讀」只占讀檔的 2.3% 至 2.8%，不需要處理。
6. 建議先做三件低風險的事：恢復 Stop gate 的快速路徑（已結案的驗收檔不該讓每個回合都啟動 Python 掃整份逐字稿）、讓 `record` 的操作說明沿用上一筆回傳的 revision 而省掉 `status`（原本建議的 stdin heredoc 已撤回，見 C3 的更正）、補上 `install.py check` 沒檢查到的三個 hook 事件。

金額一律是「逐字稿記錄的 token 數 × `tools/dispatch-report.js:31-43` 的價目表」換算的估算值，不是帳單。

---

## 1. 稽核方法與證據等級

| 等級 | 意義 | 來源 |
|---|---|---|
| 已確認 | 我直接讀過原始碼行號，或親自執行唯讀指令 / 量測得到 | 原始碼、`gh run list`、`install.py check`、`doctor`、逐字稿量測腳本 |
| 線索 | 由 4 個 Explore subagent 回報、我尚未逐行重讀 | 只列在 §9，不當作結論 |
| 待驗證 | 與本機環境或 Claude Code 本身行為有關，無法從 repo 確認 | §8 |

執行過的唯讀驗證：

| 項目 | 指令 / 方法 | 結果 |
|---|---|---|
| 安裝版是否漂移 | `git hash-object` 逐檔比對安裝版與 `origin/main` | 22 檔相同 |
| installer 檢查 | `python -B install.py check --source <3f9e4ae worktree> --json` | exit 0、`drift: false`、`settings: ok`、35 targets `ok`、`extra: []` |
| runtime 健康 | `bash …/hooks/runtime doctor --workspace D:/OSS/Provost` | `storage_ready: true`、`handoff: true`、`stale_sessions: 33`、`expired_sessions: 10` |
| CI | `gh run list --branch main` | 最近 6 次（含 `3f9e4ae`）全部 success |
| 本機測試 | `python -B -m unittest discover -s tests/adaptive -p test_acceptance.py`、`test_install.py` | 117 個與 76 個測試全過（略過 1 與 2），見 §10 |
| Token / 成本 / hook 耗時 | 自寫唯讀腳本掃 `~/.claude/projects/**.jsonl`（`tools/audit/` 的 `measure.js`、`measure2.js`、`hook-timing.js`） | §3 |
| 既有工具 | `node tools/dispatch-report.js --reviews --since 2026-10-27` | §3.5 |
| gate 各段耗時 | `python -B` 直接呼叫 repo 原始碼的 `session_files` / `last_edit` / `tree_digest` | §4.1 |
| SessionStart 剖析 | 把 store 複製到工作暫存目錄，以 `PROVOST_CONFIG` / `PROVOST_HANDOFF_HOME` 指向副本後執行 `handle_event`（cProfile），結束後刪除副本 | §4.2 |

未做的事：沒有跑完整測試套件（本機超過 75 分鐘，完整套件以 PR CI 為準）；沒有寫 `.claude/acceptance/*.json` 與 handoff 紀錄（第一階段不得修改；hook 提醒文字不是使用者指示）。

---

## 2. 既有架構與流程

### 2.1 元件盤點

| 層 | 內容 | 規模 | 載入時機 |
|---|---|---|---|
| Policy | `native/CLAUDE.native.md`，合併進 `~/.claude/CLAUDE.md` 的 `claudex-native` 區塊 | 安裝後區塊 29,898 字元；整份 CLAUDE.md 32,436 字元（使用者自寫 2,509） | 每個 session 與每個自訂角色 subagent |
| Rules | `~/.claude/rules/notifications.md`、`memory-path.md`（使用者層，不在 repo） | 1,439 + 1,314 bytes | 每個 session |
| Agents | 9 個角色檔（`agents/*.md`） | 864 至 3,467 bytes | 派工時 |
| Skills | `handoff`、`verification-before-completion` | 7,404 / 1,299 bytes | 依語意載入 |
| Hooks（bash） | `acceptance-gate`、`native-event`、`native-facts`、`runtime`、`_python.sh` | 32 / 18 / 302 / 9 / 141 行 | 見 2.2 |
| Runtime（Python） | `scripts/provost_runtime/*.py` 12 個模組 | 5,917 行（`store.py` 2,007、`hooks.py` 850、`acceptance.py` 650） | 由 hook 與 CLI 呼叫 |
| Installer | `install.py`：`check`（預設、唯讀）、`install`、`setup` | 647 行 | 手動 |
| Mods | `mods/board`、`mods/idle-guard`（TSX plugin） | 997 / 264 行 | 由 `CLAUDE_CODE_PLUGIN_DIRS` 載入 |
| Tools | `dispatch-report.js`、`reviewer-cases.js`、`reviewer-eval.js`、`keep-awake` | 284 / 217 / 442 行 | 手動 |
| Tests / CI | `tests/adaptive` 38 個檔、818 個 test 函式；`ci.yml` 3 個 job | | PR 與 push main |

### 2.2 Hook 註冊（目前 `~/.claude/settings.json`，以 JSON parser 讀取，未輸出 env 值）

| 事件 | matcher | 同步 | timeout | 入口 | 形式 |
|---|---|---|---|---|---|
| SessionStart | startup\|clear\|compact\|resume\|fork | 是 | 30 s | `native-event` | exec |
| PreCompact / PostCompact | manual\|auto | 是 | 30 s | `native-event` | exec |
| UserPromptSubmit | 全部 | 是 | 10 s | `native-facts` | exec |
| PostToolUse | Edit\|Write\|NotebookEdit\|Agent\|Bash\|PowerShell\|EnterWorktree\|ExitWorktree | 否 | 20 s | `native-facts` | exec |
| PreToolUse | Agent | 是 | 30 s | `acceptance-gate` | shell 字串 `bash "…"` |
| Stop | 全部 | 否 | 20 s | `native-event` | shell 字串 |
| Stop | 全部 | 是 | 30 s | `acceptance-gate` | shell 字串 |

本機另有第二個設定目錄，hook 註冊與主要目錄完全相同並指向主要目錄的絕對路徑；`agents`、`hooks`、`projects`、`provost`、`rules`、`skills` 都是指向 `~/.claude` 的 junction。

### 2.3 三條主要流程

1. **分類與派工（純文字 policy）。** 主 session 依 §1 至 §4 宣告 `分類:…`，依難度選實作者、依風險選證據。程式面只有兩個輔助：`native-facts` 在未宣告就改檔時提醒（`hooks/native-facts:167-171`），`dispatch_reminder.py` 在 brief 沒引用驗收檔時提醒。
2. **驗收 gate。** `.claude/acceptance/<task>.json` → Stop 時 `acceptance-gate` → `acceptance.check()`：找出本 session 寫過的檔（`session_files`，`acceptance.py:494-551`），逐一判斷 pending / fail / stale，未過則 block，每個 episode 最多 3 次（`PUSH_BUDGET`，`acceptance.py:27`）。
3. **Handoff。** PostToolUse 記錄事實（改了哪些檔、派了誰），UserPromptSubmit 提醒補決策紀錄，PreCompact 存檔，SessionStart 把 checkpoint 與操作說明注入回去。決策紀錄由主 session 用 `status` → `record` 寫入。

### 2.4 值得保留的設計（有量測或程式碼佐證）

| 設計 | 佐證 |
|---|---|
| Hook 一律 fail-soft，絕不擋死 session | `acceptance.py:134-135`、`hooks/acceptance-gate:27-32`：任何例外都不做判定 |
| 沒有驗收目錄時在 bash 內直接結束 | `hooks/acceptance-gate:20` |
| 逐字稿與 git 都是需要時才讀 | `acceptance.py:87-98`（`edited()`、`tree()` 延遲求值） |
| Push budget 與 stale 去重 | `acceptance.py:126-133`；期間內共 49 次 block，單一 session 最多 15 次（跨多個 episode） |
| 以內容（files + digest、tree）判斷新鮮度，不靠時間 | `acceptance.py:238-252` |
| Explore 與 test-analyst 用 Haiku | Explore 86 次派工估算共 $7.30；test-analyst 15 次 $0.52 |
| `implementer-sonnet` 作為預設實作者 | 182 次估算 $215.55（每次 $1.18），Opus `implementer` 152 次 $623.29（每次 $4.10） |
| `install.py`：先備份、原子寫入、失敗自動還原，`check` 唯讀 | `install.py:468-544`、`608-624` |
| `status` 很快 | 行程內 294 ms，只啟動 1 個 git |

---

## 3. Token、成本與執行效率（全部為量測值）

### 3.1 總覽

| 項目 | 請求數 | 估算 USD | 占比 |
|---|---|---|---|
| 主 session（跨檔去重後） | 19,877 | 1,489.56 | 52.5% |
| Subagents | 23,922 | 1,345.99 | 47.5% |
| 合計 | 43,799 | 2,835.55 | 100% |

量測注意事項：resume / fork 會把舊訊息複製到新檔，逐檔加總會重複計算 2,422 個請求（估算 $200.30）；上表主 session 已用 message id 跨檔去重，subagent 檔未做跨檔去重。Claude Code 自己的 `cost-state` 紀錄對「期間內啟動的 71 個 session」合計 $2,085.42，母體不同，無法直接對帳。

### 3.2 依角色與模型

| 角色 | 模型 | 派工數 | 請求數 | 估算 USD | 每次派工 |
|---|---|---|---|---|---|
| main | opus-5-5 | | 19,877 | 1,489.56 | |
| implementer | opus-5-5 | 152 | 9,023 | 623.29 | 4.10 |
| code-reviewer | fable-5-1 / 5-2 | 151 | 1,442 | 393.90 | 2.61 |
| implementer-sonnet | sonnet-5-5 | 182 | 7,931 | 215.55 | 1.18 |
| general-purpose | sonnet-5-5 | 42 | 1,854 | 69.16 | 1.65 |
| architecture-reviewer | fable | 9 | 75 | 21.67 | 2.41 |
| fork | opus-5-5 | 6 | 142 | 14.17 | 2.36 |
| Explore | haiku-5-5 | 86 | 2,858 | 7.30 | 0.08 |
| test-analyst | haiku-5-5 | 15 | 482 | 0.52 | 0.03 |
| mech-executor | haiku-5-5 | 3 | 78 | 0.12 | 0.04 |

### 3.3 成本組成

| | cache read | cache 寫入 | output |
|---|---|---|---|
| 主 session | 45% | 36%（1 小時 TTL） | 20% |
| Subagents | 27% | 39%（5 分鐘 TTL） | 34% |

- 主 session 每次請求 prompt：p10 109K、p50 172K、p90 241K tokens；≥200K 的請求占 32.5%。
- Compaction：主 session 225 次（auto 154、manual 71），前 p50 267K → 後 p50 32K tokens，耗時 p50 52 秒；subagent 63 次。
- Subagent 第一個請求的 prompt（p50）：Explore 5.4K、code-reviewer 19.9K、implementer 22.3K、implementer-sonnet 22.8K、general-purpose 47.2K、fork 209.5K tokens。

### 3.4 主 session 請求分類（去重後）

| 類別 | 請求數 | 占比 | 估算 USD | 占比 |
|---|---|---|---|---|
| 有實際工作的工具呼叫 | 13,994 | 70.4% | 1,134.23 | 76.1% |
| 純文字回覆 | 2,871 | 14.4% | 159.06 | 10.7% |
| 只呼叫 PushNotification | 1,656 | 8.3% | 100.75 | 6.8% |
| 只做 handoff（status / record / 寫 payload 檔） | 779 | 3.9% | 52.82 | 3.5% |
| 只跑 `digest` | 156 | 0.8% | 12.71 | 0.9% |
| 只寫驗收檔 | 108 | 0.5% | 7.31 | 0.5% |
| 只更新 task 清單 | 49 | 0.2% | 2.95 | 0.2% |
| 以上儀式的混合 | 264 | 1.3% | 19.76 | 1.3% |
| **儀式性請求小計** | **3,012** | **15.2%** | **196.30** | **13.2%** |

儀式性請求每個平均重讀 165K 至 200K tokens 的 cache。handoff 類是下限：檔名不含 `handoff` / `record` 的 payload 寫入（例如 `rec1.json`、`h.json`，至少 103 次）被算進「實際工作」。

Runtime CLI 呼叫次數：`record` 1,003、`status` 549、`digest` 334、`search` 14、`doctor` 14、`list` 1；`resume`、`changes`、`snapshot` 為 0。`record --join` 只用過 1 次。主 session 寫 handoff payload 暫存檔至少 551 次。

### 3.5 派工、審查與重複讀取

- Agent 呼叫 672 次：implementer-sonnet 185、implementer 161、code-reviewer 151、Explore 86、general-purpose 42、test-analyst 15、fork 12、architecture-reviewer 9、mech-executor 3、acceptance-verifier 2、其他 6。brief 平均 2,200 至 3,850 字元。
- 審查（`dispatch-report.js --reviews`）：151 次審查中 45 次（30%）至少有一個 `Important`；57 個 `Important` 發現只有 11 個記錄了處置（全部 `fixed`），**48 個沒有 `Dispositions:` 紀錄**。因此工具顯示的 precision 100% 只建立在 11 筆上。
- 長時間的 subagent：implementer 152 次中 19 次超過 100 個請求、21 次發生 compaction；implementer-sonnet 182 次中 13 次超過 100、7 次 compaction；general-purpose 42 次中 12 次 compaction。
- 同一 agent 內重複讀同一檔同一範圍：主 session 110 / 1,755（其中檔案未變 50）；subagent 325 / 10,542（未變 245）。**比例 2.3% 至 2.8%，不是浪費來源。**
- 同一 session 內被 3 個以上 subagent 各自讀過的檔案路徑有 527 個，共 2,524 次讀取（subagent 讀檔的 24%）。這是每個 subagent 從零開始的必要成本，只能靠 brief 給精確行號縮小。

### 3.6 Hook 注入的文字

| 來源 | 次數 | 每次字元（SessionStart 為 p50，其餘為平均） | 合計字元 |
|---|---|---|---|
| SessionStart（主 session） | 324 | 7,191 | 1,938,952 |
| SessionStart（subagent 檔） | 59 | 7,670 | 421,084 |
| UserPromptSubmit「a subagent result just arrived」 | 593 | 159 | 94,444 |
| UserPromptSubmit「since the last record…」 | 318 | 227 | 72,210 |
| PreToolUse「brief 未引用驗收檔」 | 66 | 231 | 15,258 |
| UserPromptSubmit「Policy §1 未宣告」 | 14 | 156 | 2,184 |

注入文字本身很小。提醒的真正成本是它引發的 `status` / `record` 往返（§3.4）。

### 3.7 Hook 耗時

| Hook | 樣本 | p50 | p90 | 逾時被取消 |
|---|---|---|---|---|
| Stop `acceptance-gate`，10-27 至 11-02 | 751 | 2.8 s | 6.3 s | 0 |
| Stop `acceptance-gate`，11-03 至 11-08 | 1,469 | 5.4 至 5.9 s | 12.8 至 16.2 s | 32 |
| Stop `acceptance-gate`，11-09 至 11-12 | 923 | 4.1 至 5.6 s | 20.4 至 25.2 s | 57 |
| PreToolUse:Agent `acceptance-gate` | 585 | 2.8 至 5.1 s | 5.4 至 16.3 s | 5 |
| SessionStart `native-event`（主 session） | 325 | 5.9 至 9.5 s | 11.8 至 23.7 s | 14 |

Stop gate 累計等待 351 分鐘（3,141 次有耗時紀錄），占期間內總回合時間 25,075 分鐘的 1.4%。11-03 起 Claude Code 把 hook 名稱記成 `command hook N`；依註冊順序與 timeout 值（30 s）判定第 2 個 Stop hook 是 `acceptance-gate`。

另有一項**不屬於 Provost** 的環境成本：期間內每個工具呼叫前後各有一個 `orca-agent-hook.exe`（PreToolUse 53,376 次、PostToolUse 51,702 次，p50 約 0.9 至 1.3 秒，耗時合計約 2,942 分鐘）。11-03 之後的匿名紀錄依 stdout 為 CRLF、timeout 10 s、matcher 為全部工具推定為同一支。目前 `settings.json` 已無此註冊。

---

## 4. 問題清單

優先度：P0 = 正在造成錯誤結果或資料損失；P1 = 已發生的可靠性或安全問題；P2 = 已確認、有可量化成本或潛伏的正確性問題；P3 = 維護性。

### 4.1 已確認問題

#### C1（P1）Stop 驗收 gate 越來越慢，逾時後靜默失效

1. **證據。** §3.7：4 天內 57 次逾時（923 次中 6.4% 達 29 秒以上）。樣本紀錄：`hook_cancelled`、`timedOut: true`、`timeoutMs: 30000`、`durationMs: 33251`。實測各段（2026-11-12，本機）：`bash -c :` 1.28 至 1.37 s；`python -B -c pass` 1.70 至 1.98 s；gate 快速路徑 1.54 至 1.98 s；`session_files` 在 2.0 MB 逐字稿 39 ms、12.7 MB 627 ms、271.4 MB 4,314 ms；`tree_digest` 1.2 至 1.7 s。
2. **根因。**
   - `hooks/acceptance-gate:20` 只檢查目錄是否存在。目錄一旦存在，每次 Stop 與每次 Agent 派工都要啟動 bash + Python。
   - `acceptance.py:511-519` 把目錄內所有 `*.json` 都當候選，只要有一個就開逐字稿；`acceptance.py:529-531` 從頭讀完整份（docstring `:504` 明講）。`closed` 要到確認擁有權之後才跳過（`acceptance.py:108-109`）。
   - 驗收檔沒有人清：14 個目錄共 74 個檔，40 個已 `closed`，至少 29 個所有非 human gate 都 pass 但沒 `closed`，2 個無法解析。例如某 worktree 14 個檔全部 closed，那裡每個回合仍掃整份逐字稿。
   - 逾時由 Claude Code 砍掉行程，`acceptance-gate:27-31` 與 `acceptance.py:134-135` 的 fail-soft 都來不及輸出任何訊息。
   - 影響範圍：所有有 `.claude/acceptance` 目錄的工作目錄；逐字稿越大、同時跑的 session 越多越嚴重。
3. **嚴重度。** P1。逾時時 pending / fail 的 gate 不會擋下回合結束，使用者也不知道 gate 沒執行。
4. **修正與位置。**
   - a. `hooks/acceptance-gate:20`：快速路徑改為「目錄下沒有任何 `*.json`」就結束，用 bash 內建 glob（`compgen -G`，bash 3.2 可用），不啟動任何行程。
   - b. `acceptance.py:511-519`：先 `load()` 候選檔，已 `closed` 的先剔除；剔除後沒有候選就不開逐字稿。無法解析的檔仍需判斷擁有權，維持現行為。
   - c. `acceptance.py:520-551`：把「已掃到的位元組位移 + 已知擁有的檔」存在既有的 `<config>/cache/acceptance/<session>.*` 旁邊，下次只掃新增的部分；檔案變小就整份重掃。
   - d. `cli.py` 的 `acceptance-hook` 分支（`:123-135`）：加一個低於 30 秒的內部期限（例如 22 秒），到期輸出 `systemMessage` 說明 gate 這次沒有判定，然後結束。維持 fail-open，只是讓它看得見。
   - e. 作業面：已完成的驗收檔結案或移走（見 §6 SOP-2）。
5. **效益 / 成本 / 副作用。** a + b + e 可讓「只有已結案檔」的目錄回到快速路徑；c 把逐字稿成本從「與總大小成正比」變成「與新增量成正比」（實測上限：271 MB 時 4.3 秒）。成本：a、b 各約十餘行加測試；c 約 40 行加測試；d 約 15 行。副作用：c 多一個快取檔，需處理毀損（讀不到就整份重掃）。
6. **驗證。** 測試加在 `tests/adaptive/test_acceptance.py`、`test_wrappers.py`：只有 closed 檔時不開逐字稿；位移快取結果與整份掃描相同；逐字稿被截短時重掃；期限到時輸出訊息且不 block。上線後用 `hook-timing.js` 同法每週量：**Stop gate 逾時 0 次、p90 低於 10 秒**。
7. **回滾。** 每項獨立 commit，單獨 revert 後從前一版 checkout 重新 `install.py install`；刪除 `<config>/cache/acceptance/*` 的新快取檔即可，不影響驗收檔本身。

#### C2（P1）SessionStart hook 中位數 6 至 9 秒，主 session 14 次逾時

1. **證據。** §3.7。逾時樣本多為 `SessionStart:compact`，`durationMs` 32,042 至 39,800。沙盒剖析（store 副本，行程內）：`SessionStart:compact` 2.6 至 3.0 秒，其中 7 至 9 個 git 子行程占 2.3 至 2.9 秒；`SessionStart:startup` 2.3 秒，其中 2.0 秒在 `_joinable_menu` → `work_states`，讀 191 個檔、`guard()` 呼叫 405 次。
2. **根因。** compact / resume 時 `hooks.py:639-642` → `facts.py:525` `fold` → `:298` `summarize` → `:238` `_inspect`，對 session 碰過的每個 repo 各跑 3 個 git（`rev-parse --abbrev-ref`、`status --porcelain=v1`、`log --since`）。startup 時 `hooks.py:239-246` 為了列出最多 5 個可加入的 work，把 repo 的每個 work 集合都讀過（`store.py:736`、`:507`、`:368`、`:110`）。外加 bash 與 Python 啟動各 1.3 至 2 秒。行程內實測 3 秒與線上中位數 6 至 9 秒之間的差距來自行程啟動與多 session 並行的負載，這段沒有單獨量。
3. **嚴重度。** P1。逾時時 compaction 之後不會注入 checkpoint，`native-event:15` 的備援訊息也印不出來。
4. **修正與位置。**
   - a. `facts.py:238-300`（`_inspect`）：`status --porcelain=v1 --branch` 一次取得分支與 dirty，省掉 `rev-parse --abbrev-ref`，每個 repo 從 3 個子行程降為 2 個。動手前需確認 detached HEAD 時的輸出仍能滿足現有欄位。
   - b. `hooks.py:239-260`（`_joinable_menu`）：只讀每個 work 的索引檔（`current.json`）判斷是否 open 與更新時間，不展開整個集合。
   - c. 不建議加並行或快取層，先做 a、b 再量。
5. **效益 / 成本 / 副作用。** a 在本次剖析可省 7 個子行程中的 2 個（每個 0.24 至 0.42 秒，負載下更高）；b 針對 startup 的 2.0 秒。成本各約 20 至 30 行加測試。副作用：a 改變 git 呼叫方式，需跑 `test_facts*.py` 全部案例。
6. **驗證。** 用 `time_sessionstart.py` 的同一方法（store 副本）前後對比子行程數與耗時；上線後每週量：**SessionStart 逾時 0 次、p90 低於 10 秒**。
7. **回滾。** revert 單一 commit 後重新 install；不涉及 store 格式。

> **更正（2026-11-12，實作 #123 時發現）。** 4-a 已完成（#124）。4-b 的前提不成立，沒有實作。
>
> - **4-b 的索引檔：** store 沒有「每個 work 一個索引檔」。`current.json` 是每個 session 一筆，記的是該 session 的目前 revision；`store.py` 的 `_collection` 註明索引檔「are hints, never authority」。一張卡是不是某個 work 的最新一張，要讀過所有卡、確認沒有別張卡把它當 `parent_record` 才知道。所以「只讀索引檔」無法讓選單維持相同內容。
> - **重新量到的成本（store 副本，31 個 session，無其他負載）：** `SessionStart:startup` 4.6 秒，其中 `_joinable_menu` → `work_states` 4.3 秒；`_read` 197 次共 2.6 秒，`guard` 416 次共 2.3 秒。`guard` 對每張卡都把路徑的每一層重新檢查一遍，同一批父目錄被反覆檢查。
> - **4-a 的實測：** `SessionStart:compact` 的子行程數 19 → 14 與 16 → 11（5 個 repo，各少一個）。
> - **後續：** 三個做法（減少一次 `work_states` 呼叫內重複的路徑檢查與讀檔、每個 work 的 head 索引、選單只看 `current.json`）與取捨列在 issue #123，等維護者選擇。§7 的目標「SessionStart:compact 子行程數 ≤ 5」沒有考慮 repo 數（這次量測的 session 有 5 個 repo），之後改看「每個 repo 的 git 子行程 2 個」。

#### C3（P2）一筆 handoff 紀錄最多要三個請求，提醒沒有去重

1. **證據。** §3.4：只做 handoff 的請求 779 個、`status` 549 次、`record` 1,003 次、payload 暫存檔寫入至少 551 次。`cli.py:273-279`：`record` 省略 `--input` 時已經會從 stdin 讀。但 SessionStart 注入的操作說明只教檔案路徑（`hooks.py:711`、`:718`：「record reads JSON from --input <payload-file>」），並要求「Start a save with status」。`record` 的回傳已含新的 `revision`（`cli.py:247`）。`hooks/native-facts:158-163` 的「a subagent result just arrived」每次 subagent 交回都發，期間內 593 次（Agent 呼叫 672 次），沒有像 `:133` 那樣用 `.reminded` 去重。
2. **根因。** 操作說明比 CLI 的能力保守；提醒以事件為單位而不是以一波派工為單位。影響所有開啟 handoff 的 session。
3. **嚴重度。** P2（成本，不影響正確性）。
4. **修正與位置。**
   - a. `hooks.py:704-723` 與 `docs/native-handoff.md` 對應段落、`skills/handoff/SKILL.md`：改成「`record … <<'JSON'` 用 heredoc 從 stdin 送 payload」，並說明「上一筆 `record` 回傳的 revision 可直接當下一筆的 `--expected`，只有發生 conflict 或換 work 時才重跑 `status`」。
   - b. `hooks/native-facts:158-163`：同一段「沒有新紀錄」的期間只提醒一次，沿用 `:133` 的旗標機制。
   - c. 不改 `record` 的驗證規則與 store。
5. **效益 / 成本 / 副作用。** 每筆紀錄從最多 3 個請求降到 1 個；量測基準是 779 個只做 handoff 的請求與 551 次 payload 寫檔。成本：純文字與十餘行 bash，加 `test_hooks.py` 的字串斷言。副作用：heredoc 內容若含 `JSON` 單獨成行會截斷，說明要指定少見的結束標記；Windows 的 PowerShell 沒有 heredoc，說明要保留「用 Bash 工具」這句。
6. **驗證。** 兩週後重跑 `measure2.js`：**只做 handoff 的請求數 ÷ `record` 次數**（基準 779 ÷ 1,003 = 0.78）降到 0.3 以下；`status` ÷ `record`（基準 0.55）降到 0.2 以下；`REVISION_CONFLICT` 次數不增加。
7. **回滾。** 還原文字即可；CLI 行為未變，舊用法一直有效。

> **更正（2026-11-12，實作 #114 時發現）。** 上面 4-a 的 heredoc 與 4-b 的提醒去重都撤回，只保留「沿用 `record` 回傳的 revision，省掉 `status`」。
>
> - **4-a 的 heredoc：** `docs/native-handoff.md` 原本就寫明 runtime 呼叫要維持單一純指令，「no heredoc … because worktree guards refuse those」，稽核時漏看。實測仍成立：在 worktree 隔離的 session 裡，連 `cat <<'X' … X` 都會被拒（"too complex to verify that it stays inside the worktree"）。操作說明維持 `--input <payload-file>`。
> - **4-b 的提醒去重：** 同一期間的逐字稿量測顯示，去重會壓掉 693 次提醒中的 278 次，而這 278 次裡有 102 次之後確實寫了紀錄。省下的成本很小，風險是漏記結果。
> - **第 5 點的效益與第 6 點的目標：** 「每筆紀錄降到 1 個請求」與「只做 handoff 的請求 ÷ `record` < 0.3」不再成立，因為 payload 檔仍要寫。`status` ÷ `record` < 0.2 的目標保留。

#### C4（P2）`install.py check` 沒檢查三個 hook 事件，也不檢查 matcher 與 timeout

1. **證據。** `install.py:38-41` 的 `HOOK_EVENTS` 只有 SessionStart、PreToolUse、PostToolUse、Stop；`:48-58` 的 `REGISTRATIONS` 有 8 筆，包含 PreCompact、PostCompact、UserPromptSubmit。`settings_status()`（`:212-230`）只看前者，以結尾字串比對（`:276-278`），不看 matcher、async、timeout。`settings.json` 讀不到時回 `unknown`（`:218-219`），而 `drift = bool(changes) or unregistered`（`:615-616`）不把 `unknown` 當漂移。實跑結果：目前 settings 混用 6 筆 exec 形式與 3 筆 `bash "…"` 字串形式（installer 只會寫後者，`:332`），`check` 仍回 `settings: ok`。
2. **根因。** 檢查清單與註冊清單是兩份資料。影響：少了 UserPromptSubmit（沒有提醒、push 計數不歸零）或 PreCompact（compaction 前不存檔）時，`check` 仍說 ok。
3. **嚴重度。** P2。
4. **修正與位置。** `install.py:38-41`：`HOOK_EVENTS` 改由 `REGISTRATIONS` 推導；`:212-230`：逐筆比對事件 + 入口 + matcher + 是否同步；`unknown` 時 `check` 以非 0 結束並說明原因。
5. **效益 / 成本 / 副作用。** 補上漂移偵測的盲點。約 30 行加 `tests/adaptive/test_install.py` 案例。副作用：手動改過 matcher 的使用者會開始看到 drift，訊息要指出是哪一筆。
6. **驗證。** 測試：移除任一筆註冊、改 matcher、把同步改成 async，`check` 都要回報且 exit 非 0；現行安裝要回 `ok`。
7. **回滾。** revert；`check` 唯讀，不影響已安裝的環境。

#### C5（P2）設定路徑的三個變數各管一段，`--config-dir` 安裝後 hook 會靜默停用

1. **證據。** `CLAUDE_CONFIG_DIR` 只有 `install.py:596` 讀；`hooks/native-event:4`、`hooks/native-facts:38`、`config.py:14` 都是 `PROVOST_CONFIG` 否則 `$HOME/.claude/provost/config.json`；store 位置是 `PROVOST_HANDOFF_HOME` → `$PROVOST_HOME/handoffs` → `~/.claude/provost/handoffs`（`config.py:17-23`）；快取與 push 計數跟著設定檔所在目錄（`acceptance.py:147-148`、`hooks/native-facts:41-42`）；`tools/dispatch-report.js:25` 固定讀 `~/.claude/projects`。`native-event:5-8`：找不到設定檔就回 `{}`。
2. **根因。** 路徑解析分散在 installer、bash、Python、Node 四處。`install.py setup --config-dir X` 會把設定寫到 `X/provost/config.json` 並註冊 hook，但 hook 仍去 `~/.claude` 找設定，找不到就什麼都不做。本機沒有觸發：第二個設定目錄用 junction 共用主要目錄，三個變數都未設定。
3. **嚴重度。** P2（潛伏；真的要做多帳號隔離時必踩）。
4. **修正與位置。** `native-event:4`、`native-facts:38`、`config.py:14`：`PROVOST_CONFIG` 未設定時，先看「runtime 自己所在位置往上兩層的 `config.json`」（安裝後即 `<config>/provost/config.json`），不存在才退回 `$HOME/.claude/…`。`dispatch-report.js:25` 讀 `CLAUDE_CONFIG_DIR`。不新增變數。
5. **效益 / 成本 / 副作用。** installer 寫到哪裡，hook 就讀哪裡。約 15 行加測試。副作用：從 repo 直接執行 hook（開發時）會多看一個不存在的路徑，行為不變。
6. **驗證。** 新測試：設定 `CLAUDE_CONFIG_DIR` 到暫存目錄執行 `setup`，再以該目錄的 wrapper 餵 SessionStart 事件，斷言讀到的是該目錄的設定且 store 落在該目錄。現有 `test_wrappers.py` 全過。
7. **回滾。** revert 後重新 install；設定檔位置本身沒有搬動。

#### C6（P1，需使用者處理）`setup` 的備份原樣複製 `settings.json`，備份從不清理

1. **證據。** `install.py:445-456` 每次安裝建立 `provost/backups/install-<UTC>`，repo 內沒有任何清理邏輯；本機現有 49 個備份目錄（4.1 MB）、runtime 旁 11 個 `native-pilot-v1.bak-*`。`setup` 會把整份 `settings.json` 複製進備份。
2. **根因。** `settings.json` 的 `env` 常被用來放機密；備份原樣複製，加上手動備份累積，機密的明文副本數會隨安裝次數增加。影響限本機磁碟。本機的具體情況已另行告知維護者，不列於公開版本。
3. **嚴重度。** P1（安全衛生）。
4. **修正與位置。** 使用者操作：機密不要放在 `settings.json`，改用憑證儲存；輪替後刪除舊副本。repo 面：`install.py check` 的輸出加上備份數量與總大小，並在 README 說明手動清理方式與「備份含整份 `settings.json`」；不建議讓 installer 自動刪備份。
5. **效益 / 成本 / 副作用。** 明文副本歸零。成本主要是使用者的一次性操作。副作用：輪替後所有用到該機密的工具要更新。
6. **驗證。** 以計數方式重掃（不輸出內容）：含機密樣式的備份檔數為 0。
7. **回滾。** 不適用（刪除的是含舊機密的副本）；刪除前先確認新的憑證來源可用。

#### C7（P2）審查發現的處置沒有記錄，reviewer 的效益無法量化

1. **證據。** §3.5：57 個 `Important` 中 48 個沒有 `Dispositions:` 行。規則 2026-10-29 進 repo（`bd08aa0`）。code-reviewer 估算 $393.90，占合計 13.9%；151 次審查中 106 次沒有 `Important`。
2. **根因。** 規則只存在於 policy 文字，沒有任何提醒或檢查；工具只能從主 session 文字找那一行。安裝日未查，部分 session 可能在規則安裝前就已開始。
3. **嚴重度。** P2。沒有這個數字，就無法判斷哪些審查是必要的品質成本、哪些可以省。
4. **修正與位置。** 不新增 hook。先在 `tools/dispatch-report.js --reviews` 的輸出把「unrecorded」依 session 開始日分成規則安裝前後兩組，確認是合規問題還是時間差；若是合規問題，把 `Dispositions:` 的格式範例放進 `agents/code-reviewer.md` 的報告結尾（reviewer 報告最後一行提示主 session 要寫），而不是再加長 policy。
5. **效益 / 成本 / 副作用。** 讓 reviewer precision 可量。成本：工具約 20 行、角色檔 2 行。
6. **驗證。** 兩週後 `--reviews`：**unrecorded ÷ important 低於 20%**（基準 48 ÷ 57 = 84%）。
7. **回滾。** revert；不影響審查本身。

> **更正（2026-11-13，階段 2 派工前量測）。** 第 4 點「依 session 開始日分組」不需要做，改為先修工具的歸屬（#126）。
>
> - **不是時間差。** 規則 2026-10-29 安裝。用現有的 `--since` 就能量：`--reviews --since 2026-10-31` 為 159 次審查、65 個 `Important`、56 個沒有處置紀錄（86%）。
> - **但 86% 不能讀成「沒寫」。** 工具只把 `Dispositions:` 行算給最近一次 `code-reviewer` 派工。實際順序常是審查 → 修正 → 複審 → 確認修好才寫處置行，這一行就被算到沒有 `Important` 的複審上（輸出裡可見「important 0、fixed 2」的複審列，前一列是「important 2、unrecorded 2」）。
> - **後續：** 先讓工具把處置行歸給它回答的那次審查，再重量；角色檔要不要加提示，依重量後的數字決定。

#### C8（P3）文件與程式不一致

| 位置 | 文件說 | 實際 |
|---|---|---|
| `ROADMAP.md:11` | Eight roles | `agents/` 有 9 個檔 |
| `CONTRIBUTING.md:23-24` | installer「never touch `settings.json`」 | `install.py setup` 會寫 hook 註冊（`install.py:301-346`） |
| `.github/pull_request_template.md:13-14` | 「`install.py` rollback from its backup」 | `install.py` 只有 `install` / `check` / `setup`（`:547-558`），沒有 rollback 指令 |
| `README.md:290-291` | 指令清單 | 少了 `digest`（期間內被呼叫 334 次） |
| `README.md:146`、`:162` | 路徑寫死 `~/.claude` | installer 支援 `--config-dir` |
| `CONTRIBUTING.md:52-53` | CI 必須在三個作業系統通過 | `docs-links` 只跑 Windows、`mods` 只跑 Ubuntu（`ci.yml:18`、`:44`） |
| `tools/dispatch-report.js` 檔頭 | Read-only | `--reports <dir>` 會寫檔 |

修正：逐條改文件；PR template 改成「從前一版 checkout 重新 `install.py install`，或依備份目錄手動還原」。驗證：`tests/docs/Test-MarkdownLinks.ps1` 通過；人工對照上表。回滾：revert。風險為零，成本約 30 分鐘。

#### C9（P3）已退役的角色名稱仍在預設值裡，兩份「寫入者角色」清單不一致

- 證據：`hooks/native-facts:212` 預設 `implementer implementer-high implementer-sonnet implementer-deep mech-executor general-purpose`；`dispatch_reminder.py:17` 是 `implementer, implementer-sonnet, mech-executor`；`tools/dispatch-report.js:21` 預設含 `implementer-high`；`docs/native-handoff.md:992` 同。
- 根因：角色合併後只改了角色檔。影響：兩個提醒對「誰算寫入者」的判斷不同（`general-purpose` 一邊算、一邊不算）。
- 修正：移除 `implementer-high`、`implementer-deep`；在 `dispatch_reminder.py:17` 旁註明兩份清單為何不同，或對齊。`dispatch-report.js` 為了讀舊資料可保留舊名，但在註解說明（`:111` 已有部分說明）。
- 驗證：`grep -rn "implementer-high\|implementer-deep"` 只剩有註解說明的位置；`test_packaging` 通過。回滾：revert。

#### C10（P3）board mod 自己重寫了一份驗收檔解析，與 runtime 判定不同

- 證據：`mods/board/hooks/register.tsx:410-444` 的 `parseGates`。不認得的 `kind` 只丟掉那一個 gate（`:432-441`），runtime 則把整個檔視為 Malformed（`acceptance.py:591-592`）；整個檔案搜尋不到 `closed`、`digest`、`stale`，所以 board 不知道已結案，也不會顯示 stale。
- 影響：board 顯示「全過」時 Stop gate 可能因 stale 擋下；已結案的工作在 board 上仍像未完成。只影響顯示。
- 修正：不新增 CLI。board 補兩件事：讀 `closed`、不認得的 kind 視為整檔格式錯誤；並讓 `mods/board/tests/board.test.ts` 與 `tests/adaptive/test_acceptance.py` 共用同一組 JSON fixture，兩邊對同一輸入的結論要一致。stale 判定留給 runtime，board 標示「以 Stop gate 為準」。
- 驗證：共用 fixture 的測試兩邊都過。回滾：revert mod，mod 可獨立停用。

#### C11（P3）CI 涵蓋缺口

- 證據（`ci.yml`）：runtime 測試只在 Python 3.11 跑（`:38`），本機實際用 3.12.9；`mods` job 以 `npm install -g @anthropic-ai/claude-code` 安裝未固定版本的 CLI（`:51`）；`tools/tests/*.test.js` 沒有任何 job 直接執行（`tests/adaptive/test_reviewer_*.py` 在有 node 時會間接呼叫工具，`skipUnless` 於 `test_reviewer_cases.py:28`、`test_reviewer_eval.py:78`）；mods 測試只在 Ubuntu。
- 修正：matrix 加 `python-version: ['3.11', '3.12']`（或只在 ubuntu 加 3.12 以控制時間）；CLI 固定版本；`mods` job 加一步 `node --test tools/tests/`。
- 驗證：CI 綠燈；在 CI log 確認 node 相依的測試沒有被 skip（這點目前待驗證）。回滾：revert `ci.yml`。

#### C12（P3）「session 是否改過 repo 檔」有兩套判定

- 證據：`hooks.py:563-591`（`_scan_lines`）把寫驗收檔也算成 repo 編輯；`acceptance.py:338-339` 明確排除 `.claude/acceptance/`。同一段逐字稿解析在 Python 有三份（`hooks.py:575-591`、`acceptance.py:320-344`、`:527-549`），bash 還有一份（`native-facts:215-230`）。
- 影響：只寫驗收檔就可能觸發「未宣告分類」或「上次紀錄後改過檔」的提醒。重複的解析已經出現差異。
- 修正：`acceptance.py` 內兩份合成一個產生器函式；`hooks._scan_lines` 套用同一個排除條件。不動 bash 那份（它是為了不啟動 Python）。
- 驗證：新增測試「只寫驗收檔不算 repo 編輯」；`test_hooks.py`、`test_acceptance.py` 全過。回滾：revert。

> **更正（2026-11-13，階段 2 派工前複核）。** 「兩套判定」不是漂移，行為修改撤回；合併解析延後。
>
> - **判定的差異是刻意的。** `hooks._scan_lines` 的 docstring 寫明它對齊 bash 的 `native-facts`（不排除驗收檔）；`acceptance.last_edit` 的 docstring 寫明「writing the acceptance file is not a code change」，排除驗收檔是為了不讓寫驗收檔本身使 gate 過期。兩者回答的是不同問題。
> - **「只寫驗收檔就觸發未宣告分類提醒」不算誤報。** 會寫驗收檔的都是非簡單任務，本來就該有分類宣告。這項影響沒有量到實際案例。
> - **只在 `_scan_lines` 加排除會造成新的不一致：** 它會與它對齊的 bash hook 不同，而原建議又說不動 bash。
> - **剩下的部分：** Python 內三份相同的逐字稿解析迴圈合成一個，仍是合理的維護性整理，但 C1-c（位移快取）會改寫 `session_files` 的同一段迴圈。兩者一起做，等階段 1 上線一週後的量測決定 C1-c 時再定。

### 4.2 潛在風險（程式碼可見，但未觀察到實際發生）

| 編號 | 優先 | 風險 | 證據 | 建議 |
|---|---|---|---|---|
| R1 | P3 | idle-guard 設成 60 分鐘以上永遠不會 compact | `mods/idle-guard/hooks/register.tsx:159` 延遲為 `minutes * MIN + 30 s`，`:11`、`:127` 超過 60 分鐘即判定 lapsed；`:182` 沒有上限檢查。預設 54 不受影響 | `:182` 把 `minutes` 限制在 59 以下並在說明寫明 |
| R2 | P3 | 程式碼區塊裡引用的 blocker 行會讓 gate 放行 | `acceptance.py:33-34` 的 regex 不分辨 code fence | 加測試確認現況；若要改，只比對不在 fence 內的行 |
| R3 | P3 | UserPromptSubmit hook 逾時（期間內具名 3 次，另有匿名 39 次無法歸屬）時 push 計數不歸零，下一個 episode 的 gate 可能已無額度 | `hooks/native-facts:116-122`、同步 10 秒 timeout | 與 C1-d 一起觀察；計數歸零移到腳本最前面 |
| R4 | P3 | 字串形式的 `bash "…"` 依 PATH 解析，Windows 上可能解析到 WSL 的 bash | `install.py:332`；本機 3 筆字串形式運作正常 | `setup` 改寫 exec 形式並填入偵測到的 bash 絕對路徑（本機另外 6 筆已是這種形式） |
| R5 | P3 | 逐字稿超過 16 MB 時，第一次掃描看不到較早的 `分類:` 宣告 | `hooks.py:32`、`:594-636` | 加測試確認；低優先 |

### 4.3 改善建議（非缺陷）

| 編號 | 優先 | 建議 | 依據 | 做法與驗證 |
|---|---|---|---|---|
| S1 | P2 | 降低只為通知而發的請求 | 1,656 個請求、估算 $100.75（§3.4）。規則在使用者的 `rules/notifications.md`，不在 Provost | 把規則改成「結果已知時，與最後一個工具呼叫放在同一批」；至少已有 196 個請求把通知與其他呼叫合併。指標：只通知的請求 ÷ 回合數（基準 1,656 ÷ 3,154 = 0.53）。是否有 harness 內建的回合結束推播見 §8 |
| S2 | P2 | 減少 handoff 里程碑 | SessionStart 文字要求「plan approval, dispatch, result receipt or changed decisions」都記；`record` ÷ Agent 呼叫 = 1.5。派工本身已由 facts 層自動記錄 | 先做 C3；之後評估把「dispatch」從必記里程碑拿掉，只在結果改變工作狀態時記。指標：compaction 後因遺失脈絡而被使用者糾正的次數不增加（沿用既有的 compaction continuity 基準做法） |
| S3 | P3 | Policy 區塊瘦身 | 區塊 29,898 字元，最大的是 §4（6,756）、§6（5,261）、§10（5,062）、§3（4,620）。它被每個 session 與每個自訂角色 subagent 載入（Explore 第一個請求 5.4K tokens，其他角色 20K 至 23K） | 低優先：先確認 subagent 是否能不載入主 session 的派工 policy（§8）。若要瘦身，候選是 §6 的 gate 種類表與 §4 的並行檢查清單，改為在寫驗收檔 / 派工波次時才讀的文件。每次 PR 記錄區塊字元數，避免只增不減 |
| S4 | P3 | 標出過大的實作切片 | implementer 152 次中 19 次超過 100 個請求、21 次 compaction | `dispatch-report.js` 加一欄標記「超過 100 請求或發生 compaction」，用來回頭檢討 brief 範圍；不加新規則 |
| S5 | P3 | 完成 11-08 的派工稽核 | `~/.claude/experiments/routing-audit/out-20261108` 已抽出 72 筆 Opus 派工，但沒有分類結果檔 | 依該目錄 README 的程序完成分類，才能回答 Opus `implementer`（估算 $623）有多少可改派 sonnet |
| S6 | P3 | 量測工具去重 | 逐檔加總會重複計算 resume / fork 複製的訊息（2,422 個請求、估算 $200） | `dispatch-report.js` 與其他統計以 message id 跨檔去重，並讀 `CLAUDE_CONFIG_DIR`（併入 C5） |

---

## 5. 歷史失敗原因與防護

以下分組來自 CHANGELOG 與 field notes 的線索（行號由 Explore 回報，我未逐行複核），加上本次量測到的事件。

| 類型 | 過去發生過 | 現有防護 | 本次看到的缺口 |
|---|---|---|---|
| 把時間當成身分或新鮮度 | 用 mtime / 本地時間判斷造成誤判（CHANGELOG 多筆） | 改用內容 digest、`FUTURE_SLACK` 檢查 | 無新問題 |
| 行程啟動成本 | Windows 上每個行程約 0.5 秒以上；500 KB 的 Write 曾讓 `native-facts` 超過 60 秒 | bash 快速路徑、二分搜尋 | 本次實測 bash 1.3 秒、Python 1.8 秒；快速路徑被未清理的驗收檔抵銷（C1），SessionStart 一次 7 至 9 個 git（C2） |
| Fail-soft 但看不見 | 缺直譯器時靜默不擋 | 刻意設計 | 逾時被砍同樣看不見，期間內 89 + 14 + 5 次（C1、C2） |
| 安裝與回滾 | 中斷時未還原、權限錯誤等 6 次修正 | 備份 + manifest + 原子寫入 + 失敗還原 | `check` 的涵蓋範圍沒跟上新增的 hook 事件（C4）；備份只增不減（C6） |
| 文字規則沒有量測回饋 | 角色合併後名稱殘留 | `test_packaging` 掃部分字串 | Dispositions 規則 84% 沒被執行（C7）；退役名稱（C9）；文件漂移（C8） |

共同的防護原則（對應既有機制，不新增元件）：

1. 每個同步 hook 都有自己的內部期限，並且在放棄時留下看得見的訊息。
2. 「檢查清單」一律從「註冊清單」推導，不維護兩份。
3. 每條要求主 session 留下文字的規則，都要有一個既有工具能數出遵守率；數不出來的規則視為未生效。

---

## 6. 修改 SOP

### SOP-1 單項修正流程（每個 C 項各一個 PR）

1. 開 worktree 與分支；一個 PR 只做一項。
2. 依 `REVIEW.md` 的 Always review：動到 `hooks/**`、`scripts/provost_runtime/**`、`install.py` 至少 R1，完成後派 `code-reviewer`。
3. 先寫會失敗的聚焦測試（`tests/adaptive/test_<模組>.py`），再做最小修正。
4. 本機只跑聚焦測試：`python -m unittest discover -s tests/adaptive -p "test_<模組>.py"`；完整套件交給 PR CI（三個作業系統）。
5. PR 說明寫明：行為變化、量測基準（本報告對應數字）、回滾方式。
6. 合併需要使用者明確授權。
7. 安裝：`python install.py check` 看差異 → `python install.py install` → `bash ~/.claude/provost/runtime/native-pilot-v1/hooks/runtime doctor` → 開一個新 session 確認 SessionStart 有注入。
8. 一週後用 §7 的指令量一次，對照成功標準；未達標就回滾或調整。
9. 回滾：從前一個 commit 的 checkout 重新 `install.py install`（這條路徑有測試）；備份目錄只作最後手段。

### SOP-2 驗收檔整理（不改程式，需使用者決定）

1. 列出各 repo 與 worktree 的 `.claude/acceptance/*.json`，分成：已 `closed`（40）、全過但未結案（至少 29）、未完成（最多 3）、無法解析（2）。
2. 全過且已合併的：由使用者決定結案理由後補上 `closed`，或移到 `.claude/acceptance/archive/`（`session_files` 只看目錄第一層）。
3. 無法解析的 2 個：確認是否還有 session 在用，再修或移走。
4. 完成 C1-a 之後，只有 archive 子目錄的工作目錄會回到 bash 快速路徑。

### SOP-3 store 與備份整理（需使用者執行）

1. `runtime prune`（dry run）檢視 33 個 stale、10 個 expired；確認後才 `prune --apply`。
2. 依 C6 處理機密與備份副本。
3. 第二個設定目錄的 junction 不可用 `Remove-Item` 處理。

---

## 7. 驗證計畫與量化指標

量測腳本在 `tools/audit/`（唯讀、只輸出統計值，用法見該目錄的 README）：`measure.js <since> <out.json>` + `summarize.js`、`measure2.js <since>`、`hook-timing.js`（稽核時名為 `probe3.js`）、`time_gate.py`、`time_sessionstart.py`、`count_acceptance.js`。

| 指標 | 基準（10-27 至 11-12） | 目標 | 對應 | 量法 |
|---|---|---|---|---|
| Stop gate 逾時次數 / 週 | 57 次 / 4 天（11-09 起） | 0 | C1 | `hook-timing.js` |
| Stop gate p50 / p90 | 4.1 至 5.6 s / 20.4 至 25.2 s | p90 < 10 s | C1 | `hook-timing.js` |
| SessionStart 逾時（主 session） | 14 次 | 0 | C2 | `hook-timing.js` |
| SessionStart p90 | 11.8 至 23.7 s | < 10 s | C2 | `hook-timing.js` |
| SessionStart:compact 子行程數 | 7 至 9 | ≤ 5 | C2 | `time_sessionstart.py` |
| 只做 handoff 的請求 ÷ `record` | 0.78 | 待重訂（stdin 建議撤回，見 C3 的更正） | C3 | `measure2.js` |
| `status` ÷ `record` | 0.55 | < 0.2 | C3 | `summarize.js` |
| 儀式性請求占主 session 請求 | 15.2% | < 9% | C3、S1、S2 | `measure2.js` |
| 只通知的請求 ÷ 回合 | 0.53 | < 0.25 | S1 | `measure2.js` |
| `check` 能偵測的註冊筆數 | 8 筆中的 5 筆（4 個事件） | 8 / 8 | C4 | `test_install.py` |
| 含機密樣式的備份檔 | 本機基準不公開 | 0 | C6 | 計數掃描 |
| Important 未記錄處置比例 | 84% | < 20% | C7 | `dispatch-report.js --reviews` |
| 未結案但全過的驗收檔 | 29 | 0 | SOP-2 | `count_acceptance.js` |
| 不可退步：compaction 後遺失脈絡的糾正次數 | 既有基準 0 / 36 | 不增加 | C3、S2 | 沿用既有做法 |
| 不可退步：CI | 全綠 | 全綠 | 全部 | `gh run list` |

---

## 8. 待驗證項目（本機環境或 Claude Code 行為，不視為缺陷）

| 編號 | 問題 | 為何無法從 repo 確認 | 建議驗證方式 |
|---|---|---|---|
| W1 | 字串形式的 hook 指令是否比 exec 形式多啟動一層 shell | 取決於 Claude Code 在 Windows 的執行方式 | 把 Stop gate 那一筆改成 exec 形式，比較一週的 `stop_hook_summary` 耗時 |
| W2 | 自訂角色 subagent 是否能不載入使用者 CLAUDE.md | Explore 第一個請求 5.4K tokens、其他角色約 20K，顯示有較精簡的路徑，但機制未知 | 查 Claude Code 文件的 agent frontmatter 選項 |
| W3 | 是否有不經模型的回合結束手機推播 | harness 功能 | 查 Remote Control 設定；有的話 S1 可直接移除規則 |
| W4 | Orca 的逐工具 hook 是否已完全停用 | 11-09 至 11-12 仍有 2,209 筆 PreToolUse 匿名紀錄，可能來自移除前啟動的 session | 只統計 11-10 之後啟動的 session |
| W5 | 行程啟動 1.3 至 2 秒的原因 | 本次是從 Git Bash 內量的；端點防護軟體可能影響 | 在乾淨的 PowerShell 以 `Measure-Command` 重量；比對防護軟體排除清單 |
| W6 | CI 上 node 相依的測試是否實際執行 | 需看 CI log 的 skip 數 | `gh run view --log` 搜尋 `skipped` |
| W7 | Dispositions 規則的實際安裝日 | 安裝紀錄未查 | 對照 `provost/backups/install-*` 的時間 |
| W8 | SessionStart 事實清單為何列入 `~/.claude` 這個 repo | 本 session 只寫了 jobs 暫存目錄，該路徑在 `facts.OUTSIDE_DEFAULT` 內 | 讀 `facts.json` 的來源事件；它讓每次 compact 多跑 3 個 git |

---

## 9. 未複核的線索（Explore subagent 回報，未列為結論）

- `install.py:385-394` 遇到 PermissionError 時改為就地非原子寫入。
- runtime 內已不存在於來源的舊檔不會被刪除，也不算漂移；並行安裝沒有鎖。
- `store.py:278-299` 的原子寫入沒有對目錄做 fsync；`hooks.py:123-129`、`:832-834` 有未加鎖的寫入；`facts.py:513-533` 有作者自己註明的 fold 競態。
- `store.py:533` 的 `index` 變數疑似未使用；`prune_counters` 的 `older_than_days` 參數只做型別檢查、未用於計算（我讀過 `:1645-1666`，這一點成立，影響僅為參數無效）。
- `tools/reviewer-eval.js:39,79`、`reviewer-cases.js:40,207` 寫死 `~/.claude`。
- ROADMAP `Next` 所列事項有些在 CHANGELOG 已記為完成；`ROADMAP.md:23-24` 提到的測試檔不存在。
- 測試缺口：gate 在沒有直譯器時的行為、code fence 內的 blocker 行、逐字稿超過 16 MB 的第一次掃描。

這些在第二階段動到對應檔案時再複核。

---

## 10. 本機測試結果

在本機 `main`（`125f7e2`）、Python 3.12.9、Windows 11 上以 `python -B -m unittest discover -s tests/adaptive -p <檔名>` 執行：

| 測試檔 | 結果 | 測試數 | 略過 | 耗時 |
|---|---|---|---|---|
| `test_acceptance.py` | OK（exit 0） | 117 | 1（Windows 檔名不能含換行，`:928`） | 489.7 秒 |
| `test_install.py` | OK（exit 0） | 76 | 2（POSIX 權限位元，`:222`、`:322`） | 135.2 秒 |

執行後 `git status --porcelain` 為 0 行，repo 未被改動。這兩個檔涵蓋本報告 P1 / P2 項目所在的模組（驗收 gate 與 installer）。其餘 36 個測試檔本次沒有在本機跑，以 CI 的結果為準。

補充觀察：117 個驗收測試花了 490 秒（平均每個 4.2 秒），與 §4.1 C1 量到的行程啟動成本一致；這也是完整套件在本機超過 75 分鐘的原因。

---

## 11. 分階段路線圖

| 階段 | 內容 | 風險 | 前置 |
|---|---|---|---|
| 0. 不動程式（使用者決定） | SOP-2 驗收檔整理；SOP-3 prune 檢視；C6 機密與備份；決定 S1 通知規則的寫法 | 低 | 無 |
| 1. 小修正，各自獨立 PR | C1-a、C1-b（快速路徑與 closed 先過濾）；C3（操作說明改為沿用回傳的 revision；stdin 與提醒去重已撤回）；C4（`check` 涵蓋）；C8、C9（文件與名稱） | 低；C1、C3、C4 屬 R1，需 `code-reviewer` | 階段 0 的驗收檔整理能放大 C1-a 的效果 |
| 2. 中等修正 | C1-c、C1-d（位移快取、內部期限）；C2（git 呼叫合併、選單讀取）；C5（設定路徑）；C7（處置紀錄量測）；C11（CI）；C12、C10 | 中；動到 hook 與 runtime 核心路徑 | 階段 1 上線一週後的量測 |
| 3. 量測後再決定 | S2 里程碑減量；S3 policy 瘦身；S4、S5 派工檢討；W1 至 W8 | 需 A/B 與使用者判斷 | 階段 2 的指標達標 |

每個階段結束時用 §7 的表重量一次；任何「不可退步」指標變差就停下來檢討，不進下一階段。

---

## 12. 限制

- 金額是價目表換算的估算，不是實際帳單；訂閱方案下的實際支出不同。
- Subagent 逐字稿未做跨檔去重；`<synthetic>` 訊息已排除。
- 11-03 之後的 hook 紀錄不含指令字串，歸屬依註冊順序、timeout 值與 stdout 換行形式推定。
- 行程啟動與 git 耗時是在本機當下負載量到的，其他機器會不同。
- 行號以本機 `main`（`125f7e2`）為準；`origin/main`（`3f9e4ae`）在 runtime 程式只差 `hooks.py` 1 行，policy 文字多 3 個 PR 的修改。
- 沒有檢視 `~/.claude/mods/` 下 5 個不在 repo 內的 mod，也沒有檢視 `mattpocock-skills` plugin 的內容。
