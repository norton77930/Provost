> Implementation evidence and pending live acceptance: `docs/analysis/adaptive-implementation-progress.md`.
> Original planning checklist below is retained; use the ledger for current status.

# 個人全域委派與 Handoff 首版實作計畫

> **For agentic workers:** 使用 `superpowers:executing-plans` 逐項執行；若使用者另外選擇委派，使用 `superpowers:subagent-driven-development`，仍遵守本 repo 一個 active writer、reviewer 唯讀及使用者已批准的審查範圍。本文不是執行授權。所有 checkbox 目前均未執行。

**Goal:** 提供使用者明確啟用的 Claude Code 協調模式與本地交接保存，使 main 可按需求來源安排有界委派，壓縮後能核對並接續工作。

**Architecture:** 保留現行 Tier 1 作為未啟用時的行為；新政策以獨立文件注入，同次只選一套政策。Python 標準函式庫 runtime 處理個人設定、session 識別、原子保存與載入；main 負責語義紀錄、來源適配、排程與現況核對。Hook 不解析任意 spec，也不啟動模型整理全文。

**Tech Stack:** 候選採 Python 3.11+ 標準函式庫、既有 Bash plugin 入口、Markdown policies／skills、Python unittest 與既有 PowerShell 檢查。Python 是新模式的明示選用依賴，舊模式不可因此要求安裝 Python。

**Spec:** [已確認的設計與相容性分析](../../analysis/2026-09-21-orchestration-handoff-design.md)。本文提出具體檔案及介面，供審閱後定案；不是已實作 API。

## Global Constraints

- 政策是個人全域設定；不要求消費專案新增可分享的 Provost 設定。
- 新功能由使用者明確啟用；既有使用者更新後不自動切換。
- 試行限定使用者選定的 Chrome Extension 工作區；本輪不修改該專案或個人目錄。
- 使用者需求、驗收與任務拆分先確認；執行分派可調整，交付／需求／驗收／範圍／重要設計變更需再確認。
- 個人政策與既有專案限制衝突時，先遵守專案限制，指出衝突並繼續可做的工作。
- 本 repo 實作時維持一個 active writer，不因正在開發平行功能就自行放寬。
- 實作預設 Opus／high、符合已確認條件才 Sonnet／high；main 模型仍由使用者選擇。這是 opt-in 試行政策，不替換舊角色模型。
- 新模式排程上限預設 2，第三個獨立就緒任務可至 3 並說明原因；不是 runtime 檔案鎖或安全邊界。
- 不自動建立 worktree。若無法在已授權工作區安全平行，使用序列執行；既有且已授權的 worktree 可辨識並隔離交接。
- Handoff 放專案本地、不進 Git；保留歷史，不自動刪除。不是原始碼或未提交變更備份。
- 保存失敗不阻擋壓縮；不得回傳 PreCompact block 或以失敗為由阻止正常壓縮。
- 不安裝／升級 spec-kit、OpenSpec、Matt skills；不更新 vendored skills，不改 Tier 2 的治理保證。
- 不新增 commit、branch、remote、release、部署或遠端 issue 操作；後續試行的環境寫入另有啟用步驟。
- 本次僅撰寫計畫；以下測試、探測與命令均待實作授權後執行。

## Review Focus

1. 同路徑前綴的不同專案、同名功能、worktree 及 fork session 不得串用紀錄：Task 1、3、5。
2. 原子寫入中斷、重複 Hook、破損索引不得發布半份或較舊的狀態：Task 3、4。
3. 未啟用、缺 Python、設定損壞及既有 plugin／個人 policy 並存不能靜默切換政策：Task 1、5、8。
4. 通知 JSON 合法但 terminal 不顯示、快取亦不可寫時，不能假稱已通知或可去重：Task 0、4、5。
5. task 勾選、OpenSpec archive、來源無法讀取或舊 worker ID 不得被視為已驗證／可重新派工：Task 2、5、6。

## 1. 分段交付與檔案責任

三個里程碑共用這份計畫，避免把首版拆成無法獨立驗收的零碎改動：

| 里程碑 | 任務 | 可驗收結果 |
|---|---|---|
| M1 協調政策 | 0–2 | 有明確相容性依據、全域 opt-in 解析與可審閱的新派工規則 |
| M2 交接 runtime | 3–5 | 紀錄、快照、失敗處理、Hook 路由及明確接續可測 |
| M3 整合及試行準備 | 6–8 | 三流程案例、跨平台檢查、安裝／回退說明及試行報告模板 |

建議本 repo 依序實作，無需為計畫創建 worktree。M1/M2 的程式介面有依賴，不為了驗證平行而強行同時修改。

| 路徑 | 動作與責任 |
|---|---|
| `scripts/provost_runtime/__init__.py` | 新增套件入口，無副作用 |
| `scripts/provost_runtime/config.py` | 個人設定驗證、精確工作區匹配 |
| `scripts/provost_runtime/store.py` | 已驗證的紀錄／快照格式、原子寫入、讀取與去重 |
| `scripts/provost_runtime/notifications.py` | 每 session／原因的失敗及恢復通知狀態 |
| `scripts/provost_runtime/hooks.py` | Hook 輸入輸出、事件分派、policy 與恢復 context |
| `scripts/provost_runtime/cli.py` | `doctor/record/snapshot/list/resume/hook` 命令，不產生隱藏遠端操作 |
| `hooks/runtime` | 新增 Bash→Python 啟動器，明確 argv，不 eval |
| `hooks/session-start`、`hooks/hooks.json` | 修改政策選擇與事件接線，保留舊模式 fallback |
| `policies/adaptive-orchestration.md` | 新增 opt-in 協調規則，不把它疊在矛盾的舊規則後面 |
| `agents/adaptive-implementer.md` | 新增 opt-in bounded writer，opus/high；不改舊 implementer |
| `skills/handoff/SKILL.md` | 新增使用者呼叫的交接入口，儲存或明確接續；不覆寫 Matt 的全域 skill |
| `docs/compatibility/{spec-kit,openspec,matt-skills}.md` | 新增薄的來源適配說明，不做通用 parser |
| `docs/examples/provost-user-config.json` | 新增無個人資料的設定範例；不自動複製到 home |
| `docs/examples/adaptive-handoff-pilot.md` | 新增診斷、啟用、觀察及回退步驟 |
| `docs/analysis/adaptive-host-compatibility.md` | Task 0 建立 CLI 探測報告，不能用文件查閱冒充實測 |
| `tests/adaptive/_support.py`、`tests/adaptive/test_*.py`、`tests/adaptive/fixtures/` | 新增 stdlib 測試及合成案例；不複製私人 session |
| `tests/roles/Test-RoleDefinitions.ps1` | 補新角色 effort／opt-in 邊界檢查 |
| `.github/workflows/ci.yml` | 新增 opt-in runtime 的 Python matrix，保留原 jobs |
| `README.md`、`README.zh-TW.md`、`CONTRIBUTING.md` | 同步角色、skill 數量、依賴、限制及使用方法 |

發布版本號與 release 不在本計畫的自動步驟內。

## 2. 具體資料與啟用契約

### 2.1 個人設定

預設位置：`~/.claude/provost/config.json`。可用 `PROVOST_CONFIG` 指向另一個設定檔，這是 Provost 自己的變數，不是 Claude 內建設定。實作測試使用 temporary home／明確 config 路徑，不讀取真實個人設定。

```json
{
  "schema_version": 1,
  "mode": "adaptive",
  "workspace_roots": ["D:/work/example-project"],
  "all_workspaces": false,
  "max_writers": 2,
  "burst_writers": 3,
  "handoff": true
}
```

- 無設定或 `mode: "legacy"`：舊模式；不建立 `.provost`、不載入新角色政策。
- `mode: "adaptive"` 且目前 workspace 明確符合 allowlist，或 `all_workspaces: true`：新模式。首個試行不使用 all_workspaces。
- 匹配標準為實際 checkout root 的正規化完整路徑相等，不用字串前綴。linked worktree 視為不同 workspace，需另列，不自動包含整個 `.claude/worktrees`。
- schema 不認識、型別錯誤或 max/burst 不符合 `1 <= max <= burst <= 3`：回報設定錯誤，不啟用新模式、不寫 runtime 資料。
- 來源流程不寫死在設定檔；main 讀既有專案記憶，當次指定優先。
- `doctor` 只回報所需鍵是否有效，不印完整 Claude settings、環境變數或認證。

### 2.2 本地儲存與身分

```text
<checkout>/.provost/handoffs/v1/
  sessions/<sha256(session_id)>/
    records/<revision>.json
    snapshots/<snapshot_id>.json
    current.json
    latest.json
```

workspace 身分是正規化 checkout 絕對路徑的雜湊；branch 名稱只作提示。非 Git 目錄使用明確 cwd，且以同 workspace 規則隔離。不得追蹤 `.provost` 內導向其他目錄的 symlink／junction 來寫入；拒絕 session／work ID 中可造成路徑穿越的使用方式，實際檔名均為生成的 ID。

Git repo 的啟用步驟使用 `git rev-parse --git-path info/exclude` 找到排除檔，以 literal append 加上 `/.provost/handoffs/`，不改團隊 `.gitignore`。先檢查是否已追蹤此目錄：已追蹤就停止啟用並說明，絕不自行 untrack 或刪檔。此為後續使用者明確啟用時的本地 Git metadata 寫入，不在本輪執行；正常 Hook 不自動修改 exclude。排除未生效時拒絕寫交接並採不阻擋壓縮的錯誤路徑。

紀錄採 UTF-8 JSON。main 經 CLI 提交語義內容，runtime 填身分、時間、revision 與 transcript cursor。最小輸入：

```json
{
  "work_id": "export-feature",
  "phase": "implementation",
  "sources": [{"kind": "file", "ref": "specs/export/tasks.md", "item": "T012"}],
  "assignments": [{"task": "T012", "agent_id": "worker-1", "observed_state": "running", "write_scope": ["src/export.ts"]}],
  "decisions": ["CSV only; JSON deferred by owner"],
  "blockers": [],
  "evidence": [],
  "next": "Collect worker-1 result and validate CSV acceptance",
  "suggested_skills": ["tdd"]
}
```

外層 envelope 固定包含 `schema_version, workspace_id, session_id, revision, parent_record, recorded_at, transcript_cursor, payload`。`transcript_cursor` 僅存來源識別／大小／mtime，不複製 transcript。CLI 不接受執行命令或任意輸出路徑欄位。遠端 sources 使用完整 host/project/issue URL；不主動 fetch，也不保存登入資訊。

records 是關鍵節點的不可變短紀錄版本；snapshots 是正式交接／壓縮時的自足副本，含來源 revision 與同一條交接鏈的 parent。current/latest 都是可重建指標，不是唯一有效資料。

同 session 的 `record` 用 expected revision 做 compare-and-swap；同一 writer 的重複提交 ID 回傳已有結果，並行提交衝突需重讀後再提交。record 與 snapshot 的指標發布共用短暫 session 寫鎖。POSIX 使用 `fcntl.flock(LOCK_EX | LOCK_NB)`，Windows 使用 `msvcrt.locking(LK_NBLCK, 1)` 鎖固定一個 byte；程序退出由 OS 釋放，不做 PID／時間推測式搶鎖。lock file 保留且不 unlink，避免不同程序鎖到不同 inode。鎖忙回報錯誤，不重試迴圈阻塞壓縮。

### 2.3 明確 CLI 介面

由 `hooks/runtime` 啟動 `python -m provost_runtime.cli`，設定模組搜尋路徑為 plugin 的 scripts；Windows 搜尋 `python`／`py -3`、POSIX 搜尋 `python3`／`python`，實測版本必須 >=3.11，不下載依賴。

```text
doctor --workspace <root>                         # 唯讀能力檢查
record --workspace <root> --session <id> --expected <revision|null> --request-id <id>
                                                  # JSON stdin；回傳新 revision
snapshot --workspace <root> --session <id> --request-id <id>
list --workspace <root>                           # 只列 metadata，沒有副作用
resume --workspace <root> --session <new-id> --from <snapshot-id>
                                                  # 明確接續，回傳待核對 context
hook                                              # Claude Hook JSON stdin
```

`resume` 只接收位於相同 workspace 儲存區的 snapshot ID；不接受任意檔案路徑。新 session 接續時建立自己的紀錄鏈並指向來源，不改舊鏈，不繼承執行中 worker 的所有權。跨 checkout 接手需要重新核對／明確映射，不自動沿用絕對路徑。

人工命令成功 exit 0、輸入／儲存錯誤 exit 1，stdout 為 JSON；Hook 包裝器對保存錯誤仍 exit 0，且不輸出 block。錯誤分類使用固定代碼，不把敏感 payload 放入例外訊息。

## 3. 實作任務

### Task 0：確認 Host 能力與通知路徑

**Files:** 新增 `docs/analysis/adaptive-host-compatibility.md`；合成輸入放 `tests/adaptive/fixtures/hooks/`。

**Interfaces:** 產出實測表：CLI version、event、收到的必要欄位、可見通知方式、順序與限制。後續 Task 5 只依已觀察契約接線。

- [ ] 記錄 `claude --version`；使用獨立 temporary 設定與工作目錄，保持真實 settings 不變。探測 session 禁用所有應用程式寫入工具，任務僅回覆固定短字串。
- [ ] 先建立一個錯誤預期案例：PreCompact 返回 `systemMessage` 不能被當作使用者已看見通知；保留實際觀察。
- [ ] 以官方事件格式驗證 SessionStart 的 startup/compact/resume/clear/fork，PreCompact manual/auto 及 PostCompact。不能手動重播 JSON 就宣稱真實 auto-compact 已測。
- [ ] 以 interactive session 驗證 `terminalSequence`，並驗證 noninteractive 下不顯示的限制；後續 SessionStart 的 `systemMessage` 作補充。若指定版本無可用管道，記為限制，不能靠 exit 2 達成通知。
- [ ] 同時檢查既有個人政策／插件可能重複注入的狀況，只列來源與衝突摘要，不印完整個人設定。

探測輸入示例（測試檔，不是實際發送到使用者的通知）：

```json
{"session_id":"fixture-session","cwd":"<temporary-workspace>","hook_event_name":"PreCompact","trigger":"manual","custom_instructions":null}
```

通過標準：至少在首個目標 CLI／Windows interactive 環境確認同 session 恢復和非阻擋通知；沒有證據的事件或平台留在「未驗證」，不默默更改使用者承諾。尚無自動壓縮證據時可完成後續單元實作，但不能宣布試行驗收通過。

### Task 1：全域 opt-in 設定與工作區解析

**Files:** 新增 `config.py`、`__init__.py`、`tests/adaptive/test_config.py`、設定範例。

**Interfaces:** `load_config(path: Path) -> dict`；`resolve_mode(config: dict, workspace: Path) -> str` 回傳 `legacy` 或 `adaptive`；`workspace_key(root: Path) -> str`。無效設定拋 `ConfigError`，由 Hook 層處理。

測試共用 `_support.py` 在新增第一項測試時一併建立；不作獨立 scaffolding 任務。每份 test 先 import 此檔以設定 scripts import path。共用 fixture 不讀真實 home：

```python
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

class FixtureCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config_path = self.root / "user-config.json"
        self.config_path.write_text(json.dumps({
            "schema_version": 1, "mode": "adaptive", "workspace_roots": [str(self.root)],
            "all_workspaces": False, "max_writers": 2, "burst_writers": 3, "handoff": True
        }), encoding="utf-8")
        self.precompact_event = {"session_id": "s1", "cwd": str(self.root),
            "hook_event_name": "PreCompact", "trigger": "manual", "custom_instructions": None}

    def seed_snapshot(self, next_step):
        from provost_runtime.store import record, snapshot
        record(self.root, "s1", {"work_id": "fixture", "phase": "implementation",
            "sources": [], "assignments": [], "decisions": [], "blockers": [],
            "evidence": [], "next": next_step, "suggested_skills": []}, None, "record-1")
        return snapshot(self.root, "s1", "event-1")
```

各 test 使用 `_support.FixtureCase`，並 import 該 task 定義的函式；mock 使用 `from unittest import mock`。Git 專用案例在自己的 temporary 子目錄初始化，非 Git 案例維持上述 fixture。

- [ ] 先寫缺設定及前綴混淆測試：

```python
def test_workspace_match_is_not_a_string_prefix(self):
    cfg = {"schema_version": 1, "mode": "adaptive", "workspace_roots": [str(self.root / "app")],
           "all_workspaces": False, "max_writers": 2, "burst_writers": 3, "handoff": True}
    self.assertEqual(resolve_mode(cfg, self.root / "app-copy"), "legacy")
```

- [ ] Run: `python -m unittest discover -s tests/adaptive -p test_config.py -v`；初始預期因缺模組／行為而 FAIL，記錄原因。
- [ ] 實作型別驗證及 `Path.resolve()` 正規化；Windows 大小寫比較需依 OS 路徑語義。採完整 root equality，不以 `startswith` 判斷。
- [ ] 補 malformed JSON、未知 schema、沒有 allowlist、不同 worktree、Unicode 路徑、超出 writer 上限與 custom `PROVOST_CONFIG` 案例。
- [ ] 重跑同一命令至 PASS；測試確認未建立 workspace 資料夾或改使用者設定。

### Task 2：新協調政策與有界實作角色

**Files:** 新增 `policies/adaptive-orchestration.md`、`agents/adaptive-implementer.md`；修改 role test 與中英文 README 角色表／數量；新增 `tests/adaptive/test_policy_contract.py`。

**Interfaces:** opt-in 的派工目標 `provost:adaptive-implementer`，frontmatter `model: opus`、`effort: high`。Sonnet 分派使用該角色的 model override；不宣稱可在 Agent call 直接覆寫 effort。

- [ ] 先在 role test 加入新角色必需為 opus/high 的斷言，執行 `powershell.exe -NoProfile -File tests/roles/Test-RoleDefinitions.ps1`，確認缺角色時 FAIL。
- [ ] 新增角色，工具比照現行 writer，禁止 nested agents、越過 brief 遍歷全計畫及未授權 Git 操作：

```yaml
---
name: adaptive-implementer
description: Use only when the active Provost policy is adaptive; implement the assigned bounded brief.
tools: Read, Glob, Grep, Write, Edit, Bash
model: opus
effort: high
---
```

- [ ] 政策寫入：先確認計畫；預設委派；小修／診斷連動修正／整合例外須具體原因；依賴、範圍與可變資源三項都檢查；專案限制優先；2→3 條件；一般實作 Opus/high，bounded Sonnet/high；升級規則；重要節點 record；main 統一回寫進度。
- [ ] brief 固定提供來源、交付、驗收、寫入範圍、禁止碰觸區域、依賴、共用資源與回報格式；不給 worker 完整 session。
- [ ] 靜態契約測試只驗證角色、政策引用與設定一致；不得用字串存在宣稱模型會遵循規則。語義遵循另由 Task 6 合成案例及 Task 8 試行觀察。
- [ ] 重跑 role check 與 `python -m unittest discover -s tests/adaptive -p test_policy_contract.py -v`，確認舊角色 frontmatter 不變。

### Task 3：短紀錄及交接快照的可靠保存

**Files:** 新增 `store.py`、CLI 的 record/snapshot/list 分支、`tests/adaptive/test_store.py`。

**Interfaces:** `record(root: Path, session: str, payload: dict, expected: str | None, request_id: str) -> dict`；`snapshot(root: Path, session: str, event_key: str) -> dict`；`latest(root: Path, session: str) -> dict | None`。傳回值包含 `revision/snapshot_id`、`status` 與來源身分；schema 不合拋 `StoreError`。

- [ ] 用 TemporaryDirectory 建立合成 Git workspace 並在 fixture 排除 runtime 目錄；所有 Git 命令只用 fixture，沒有真實 repo init 或 commit。
- [ ] 先寫故障測試並執行：

```python
def test_failed_snapshot_keeps_previous_complete_snapshot(self):
    old = self.seed_snapshot(next_step="collect result")
    with mock.patch("provost_runtime.store.os.replace", side_effect=OSError("fixture failure")):
        with self.assertRaises(StoreError):
            snapshot(self.root, "s1", "event-2")
    self.assertEqual(latest(self.root, "s1")["snapshot_id"], old["snapshot_id"])
```

- [ ] Run: `python -m unittest discover -s tests/adaptive -p test_store.py -v`；先 FAIL，再實作。
- [ ] 驗證 payload 與 schema；以同目錄 temporary 檔寫 UTF-8 JSON、flush/fsync、rename/replace 發布；先發布不可變檔，再更新指標。限制單份短紀錄為 32 KiB，超出回報清楚錯誤，不靜默截斷。
- [ ] record 寫入鎖及 expected revision 比對；Hook 讀不可變 record，可接受最後完整 revision，不讀正在寫的暫存。snapshot 同事件 key 重試冪等；不同事件即使內容相同仍保有各次交接 metadata。
- [ ] Hook event key 由 session、trigger、來源 record revision 與當時 transcript cursor 雜湊組成；不使用時間戳作唯一冪等判斷。無 cursor 時只承諾 record revision 層級去重，在報告明示限制。
- [ ] 指標損壞時依有效 schema、workspace/session 身分及 parent chain 回找完整紀錄；不靠檔案 mtime 猜最新版。候選鏈分岔時回報需核對，不任意選一份。
- [ ] 補不同 session、重複 request、並行寫入衝突、半份 JSON、未知 schema、錯誤 workspace、symlink/junction、路徑穿越、已追蹤 runtime 目錄、未排除目錄、非 Git 工作區案例。
- [ ] 重跑至 PASS。測試不刪已有歷史；清理僅限 fixture 自有 temporary root。

### Task 4：非阻擋保存失敗與通知

**Files:** 新增 `notifications.py`、`tests/adaptive/test_notifications.py`。

**Interfaces:** `notice(session_key: str, error_code: str | None, state_dir: Path) -> dict` 回傳 `level: first/repeat/recovered/none` 與固定安全文字。通知 cache 放個人 Provost config 同層的 `cache/notifications/`，只存雜湊、錯誤代碼與計數，不存交接或 transcript。

- [ ] 建立 first→repeat→recovered→none 測試：

```python
def test_recovery_is_announced_once(self):
    self.assertEqual(notice("s", "WRITE_FAILED", self.root)["level"], "first")
    self.assertEqual(notice("s", "WRITE_FAILED", self.root)["level"], "repeat")
    self.assertEqual(notice("s", None, self.root)["level"], "recovered")
    self.assertEqual(notice("s", None, self.root)["level"], "none")
```

- [ ] Run: `python -m unittest discover -s tests/adaptive -p test_notifications.py -v`；確認 FAIL。
- [ ] 實作單次狀態轉換與原子 cache 保存；workspace 保存故障不依賴同一故障目錄寫通知標記。
- [ ] cache 也無法寫時，每次只顯示簡短失敗及「無法保存通知狀態」；不能承諾跨 Hook process 的去重仍有效。清理通知快取不在首版內。
- [ ] 補不同原因、不同 session、cache 損壞與拒絕寫入測試，重跑至 PASS。

### Task 5：Hook 路由、恢復及手動 Handoff

**Files:** 新增 `hooks.py`、`hooks/runtime`、`skills/handoff/SKILL.md`、`tests/adaptive/test_hooks.py`、`test_cli.py`；修改 `hooks/session-start` 與 `hooks/hooks.json`。

**Interfaces:** `handle_event(event: dict, config_path: Path) -> dict` 回傳官方 Hook JSON。CLI `hook` 保證 stdout 只有 JSON；錯誤轉為非阻擋結果。

- [ ] 先建立 `startup` 不讀舊工作、`compact` 只讀同 session、保存故障不 block 的測試：

```python
def test_precompact_failure_does_not_block(self):
    with mock.patch("provost_runtime.hooks.snapshot", side_effect=StoreError("WRITE_FAILED")):
        result = handle_event(self.precompact_event, self.config_path)
    self.assertNotEqual(result.get("decision"), "block")
    self.assertNotEqual(result.get("continue"), False)
```

- [ ] Run: `python -m unittest discover -s tests/adaptive -p test_hooks.py -v`；先 FAIL。
- [ ] SessionStart 啟動器先檢查個人 Provost config 是否存在；不存在時完全沿用舊 Bash 路徑，不呼叫 Python。存在但 runtime 不可用／設定錯誤時，顯示新模式未啟用並採舊政策；不靜默宣稱已恢復。只要設定存在而採舊政策的原因非明確 legacy，須提供原因摘要。
- [ ] 在 adaptive SessionStart context 提供 event 的 session ID、解析後的 workspace root 及 runtime 入口，main 不猜 session 身分。每次命令核對輸入與已綁定 workspace；cwd 在 repo 子目錄時，以唯讀 Git root 查詢正規化。non-Git 模式只接受明確 root，不自行向父層掃描其他工作。
- [ ] matcher 包含 startup/clear/compact/resume/fork；新模式只輸出 adaptive policy，不同時注入完整 legacy policy。舊模式只保留原有 startup/clear/compact 行為，新增 resume/fork 匹配不改其 context。
- [ ] PreCompact 保存最後完整短紀錄；沒有紀錄就回報缺口，不生成看似有效的空白成功快照。返回 exit 0，不使用 `decision: block`。失敗用 Task 0 確認的互動通知管道；不可見時留到可用 SessionStart 訊息並在報告註明。
- [ ] PostCompact 僅記錄該次 compaction 已觀察到及來源 metadata，摘要不直接取代任務狀態，也不預設複製全文。若 SessionStart 與 PostCompact 次序不同，恢復不依賴後者先完成。
- [ ] compact/resume 可載入已綁定同 session 的有效紀錄，產生「待核對的觀察」；startup/clear/fork 僅提供使用入口，不自動載入舊工作或啟動 writer。
- [ ] 注入 policy 加摘要總量目標 <=8,000 characters，超過時保留身分、限制、未核對標記及檔案引用；要求 main 開啟必要紀錄，不靜默截斷決策欄位。
- [ ] `resume --from` 明確接續來源；main 核對 tasks/code/evidence/worker，不把 source 的 observed_state 當 ownership，不自動重派。
- [ ] skill 使用 `disable-model-invocation: true`，`/provost:handoff` 保存、`/provost:handoff resume <id>` 接續。自然語言「接續上次」由 main 呼叫 list 並選擇；只有多個合理候選才問。更新既有短紀錄由 main 依 policy 直接呼叫 record，不依賴自動呼叫 user-only skill。
- [ ] 補 stdin 畸形、非互動通知、missing Python、legacy 無副作用、含空格 plugin 路徑、Git Bash path、record 在 snapshot 期間更新與 clear/fork 案例；重跑 hook/CLI tests 至 PASS。

### Task 6：三種來源適配與排程案例

**Files:** 新增三份 `docs/compatibility/` 文件、`tests/adaptive/fixtures/workflows/` 合成 Markdown、`tests/adaptive/test_workflow_contracts.py`、`tests/adaptive/workflow-scenarios.md`。

**Interfaces:** main 的派工 brief 契約為 `sources, deliverable, acceptance, dependencies, write_scope, shared_resources, blockers`；`dependencies` 區分 known-empty 與 unknown，未知不可當作無依賴。不是 runtime 的任務資料庫。

- [ ] 先寫 fixture contract 檢查，缺檔／缺預期判斷時 FAIL；再補最小合成文件。
- [ ] spec-kit 案例：兩個 feature；[P] 加單一 writer；checklist 品質 gate；RED/GREEN 合成 brief；converge 新增任務但不得擴張需求；唯讀 parser 不改 feature.json。
- [ ] OpenSpec 案例：current 加 delta；非預設 contextFiles；blocked；archive 仍有未完成警告；來源搬移；舊 CLI 欄位缺失。每案 expected 指定是否可派工及應引用哪些來源。
- [ ] Matt 案例：本地/GitHub/GitLab 相同數字不同來源；Blocked by 未完；依賴未知；沒有 paths 仍需 brief 範圍；ticket 已關閉但證據不足。
- [ ] 共用案例：兩個獨立 writer 可排程、第三個需原因、同檔或共用瀏覽器只能序列相關步驟、已完成工作不能重派、來源 gate 不得被一般自主政策略過。
- [ ] Run: `python -m unittest discover -s tests/adaptive -p test_workflow_contracts.py -v`。這只證明案例與文件契約完整，報告不得稱為 agent 行為測試。
- [ ] 獲准的實作驗收階段，以沒有寫入／派工工具的 Claude session 逐案輸出簡短排程理由及 verdict，人工對照 expected。選定模型、CLI 與案例版本一併記錄；不是可保證每次相同行為的安全測試，也不存完整私人 transcript。

### Task 7：跨平台回歸與安裝文件

**Files:** 修改 `.github/workflows/ci.yml`、`README.md`、`README.zh-TW.md`、`CONTRIBUTING.md`；新增 `docs/examples/adaptive-handoff-pilot.md`；新增 `tests/adaptive/test_packaging.py`。

**Interfaces:** 未啟用的既有 plugin 安裝仍可使用；啟用模式可透過 doctor 檢查依賴、版本、workspace、排除狀態與政策衝突。

- [ ] 包裝測試先檢查新入口、policy 路徑、manifest JSON 與 skills/roles 的雙語文件一致，缺資料時 FAIL。
- [ ] 更新 CI：Windows/Ubuntu/macOS、Python 3.11 標準函式庫 tests；保留既有 Windows governance、Linux plugin jobs，GitHub action 固定 reviewed commit，不在計畫中猜測新 action SHA。
- [ ] 執行 `python -m unittest discover -s tests/adaptive -p 'test_*.py' -v`，再跑既有 role、Markdown links、PowerShell syntax checks 及原 Linux SessionStart JSON check。Tier 2 完整既有 checks 由 CI 保留，不以新測試取代。
- [ ] 新 runtime 跨平台 CI 綠只證明合成 runtime 行為；Claude interactive hook 支援表分平台記錄「已實測／未實測」，不得把 CI 綠當 Claude 三平台皆通過。
- [ ] 文件列出 Python 選配依賴、角色數量、個人設定位置、`.provost` 排除、三種來源限制、失敗通知限制、無自動 commit／worktree／issue mutation、停用不刪歷史。
- [ ] 執行 `claude plugin validate .`；若 CLI 指令不可用或改變，記錄差異，不自行安裝／升級。

### Task 8：指定專案試行準備與回退

**Files:** 更新 `docs/examples/adaptive-handoff-pilot.md`；必要的使用者專用啟用草案只在本地 temporary 位置產生，不把個人路徑／憑證寫入公開範例。

**Interfaces:** 交付可檢查的啟用差異、doctor 結果及試行情境；此步的 repo 文件可隨計畫實作，但真實設定寫入和試行工作執行須使用者最後確認。

- [ ] 準備精確 config 草案，allowlist 只列使用者指定的 checkout root。明確區分 root 與既有 feature worktree；沒有必要不再建立新的 worktree。
- [ ] 準備個人既有 policy／plugin 衝突摘要與最小變更；不可直接在既有全域規則上再疊第二套矛盾政策，也不 wholesale 重寫 CLAUDE.md。
- [ ] 準備 runtime Git exclude 變更並確認無既有 tracked handoff。先完成可審閱的 config、policy 差異與試行情境，再請求一次啟用確認。
- [ ] 先單 writer 驗證一個真實交接循環、同 session compact、新 session 明確 resume、保存失敗回復。Chrome Extension 的單一 writer 規則仍生效時，不執行雙 writer 試行。
- [ ] 若使用者另行授權特定任務的平行例外，才用兩個已確認獨立的 brief 比較；三 writer 不是首個試行的必經步驟。
- [ ] 模型路由做兩次 bounded 派工，確認 Opus/high 預設與 Sonnet/high override 的實際結果；記錄影響路由的 env 是否存在，不印值或完整 settings。不以角色 frontmatter 存在當路由實證。
- [ ] 記錄工作總時間、等待、返工、main context、可取得的 usage 與失敗；cache token 不當成等額帳單，不承諾不同任務可直接比較成本。
- [ ] 回退：個人 config 改 legacy 或移出 allowlist，在安全工作邊界啟新 session；停止新增派工，既有 worker 先核對並收尾。停用不會撤回已注入的當前 context，也不會自動取消 worker；保留交接歷史及 exclude，使用者如需移除另行處理。

## 4. 任務依賴、完成界線及未覆蓋事項

```text
Task 0 → Task 5（Host 契約）
Task 1 → Task 2 → Task 5（政策選擇）
Task 1 → Task 3 → Task 4 → Task 5（交接資料）
Task 2 → Task 6
Task 5 + Task 6 → Task 7 → Task 8 啟用準備
```

完成 repo 實作的最低條件：Tasks 1–7 的指定檢查通過、Task 0 限制如實記錄、Task 8 的啟用差異可審閱。若真實 auto-compact／通知尚未驗證，只能稱「runtime 與合成測試完成」，不能稱交接功能完整驗收。真實專案已選定不等於已授權改它的規則或開始付費 probe。

首版不做：大型規格解析框架、全 transcript 摘要服務、自動清理歷史、自動跨電腦同步、worktree 生命週期管理、全自動 source archive／sync、模型 benchmark 平台、企業代管設定。

無法完全消除的限制：main 遺漏語義紀錄、來源在核對後又被外部修改、LLM 排程判斷失誤、整個磁碟不可寫時無通知去重、不同終端對通知的支援差異。交接只提供可追查的觀察，不宣稱交易式任務狀態或強制排程隔離。

## 5. 計畫自我檢查與審閱

- 設計 §3／§12 由 Tasks 2、6 覆蓋；§4 由 Tasks 1、2、6、8 覆蓋；§5–6 由 Tasks 3–5 覆蓋；§11 由 Tasks 1、7、8 覆蓋。
- 設計中的 worktree 授權採明確保守 fallback：不自動建立，必要時序列化；不是遺漏。
- 公開預設不改；個人 Opus/high 與 2→3 設定透過 opt-in 政策試行。
- 每個行為任務有 FAIL→最小實作→PASS 步驟。文件及人工 agent 案例與自動測試的證據層級分開。
- 這次文件只做 diff／格式與範圍檢查，不執行任何列出的測試、CLI 探測或真實試行。

審閱重點：Python 選配依賴、不可變短紀錄／快照兩層保存、同 workspace 的明確接續，以及 Tasks 0–8 的首版範圍。確認計畫後再開始行為變更；預設建議由目前 session 的單一 writer 依序實作，不強制每項都增加獨立 reviewer。

參考：[Claude Code Hooks](https://code.claude.com/docs/en/hooks)、[subagents](https://code.claude.com/docs/en/sub-agents)。官方文件會變動；Task 0 必須以目標 CLI 的實際觀察校正，不把本計畫當作 Host 的保證。
