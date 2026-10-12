# Provost

**流程輕重跟著任務走。** —— Claude Code 的編排政策、角色組與 handoff runtime。

*[English →](README.md)*

Provost 是一套每天實際在用的 Claude Code 設定的正本。主 session 先依難度和風險為每個請求分類：簡單的工作自己做，有界的 slice 交給比較便宜的 implementer 角色，只有在失敗「測試和實機都看不出來」時才找全新 context 的 reviewer。一個小型 runtime 會記下決策和派工，讓工作撐過 context 壓縮和 session 重啟。

它照實際使用的樣子公開：為一個人的工作調校、以 Windows 為主，用「複製檔案到 Claude 設定目錄＋漂移檢查」的方式安裝。

## Repo 內容

| 路徑 | 內容 | 安裝到（`~/.claude` 底下） |
|---|---|---|
| [`native/CLAUDE.native.md`](native/CLAUDE.native.md) | 編排政策 | `CLAUDE.md` 中 `claudex-native` 標記之間 |
| [`agents/`](agents/) | 8 個 subagent 角色 | `agents/` |
| [`hooks/`](hooks/)、[`scripts/provost_runtime/`](scripts/provost_runtime/) | handoff runtime 與 hook | `provost/runtime/native-pilot-v1/` |
| [`skills/verification-before-completion/`](skills/verification-before-completion/) | 宣告完成前的驗證紀律 | `skills/` |
| [`skills/handoff/`](skills/handoff/) | `/handoff` skill 參考版（不安裝，保留你自己的） | — |
| [`tools/dispatch-report.js`](tools/dispatch-report.js) | 從 transcript 統計過往派工的成本與結果 | `experiments/` |
| [`install.py`](install.py) | 安裝程式與唯讀漂移檢查 | — |

方法論 skill（TDD、除錯、grilling、spec 與 ticket）由 [`mattpocock-skills`](https://github.com/mattpocock/skills) plugin 提供，政策直接引用它們的名稱。

## 政策怎麼運作

每個非瑣碎的請求先宣告一行分類：

```text
分類:<簡單|中等|機械|困難> / <無風險|R1|R2> / 證據:<測試|實機|審查|架構> → <路徑>
```

- **難度決定誰來實作。** 簡單的留在主 session；中等的交給 `implementer-sonnet`（方法已定、驗收清楚）或 `implementer`（還有設計選擇；concurrency、lifecycle、安全相關則用 `effort: high`）；完全指定的重複工作交給 `mech-executor`；困難的工作要先有核准的 spec。
- **風險決定用什麼證據結案。** 選「最便宜、但真的抓得到失敗」的證據：聚焦測試、實機執行，或只在兩者都看不出來時派 `code-reviewer`（動到對外介面再加 `architecture-reviewer`）。檔案數、diff 大小、「看起來很複雜」明文不是審查的理由。
- **R2 先停。** 認證、密鑰、金流、migration、破壞性 public API、跨 repo 或不可逆操作，等使用者決定，並且要審查通過後才算完成。
- **writer 有預算。** 平常最多 2 個同時寫入，第 3 個要證明完全獨立；每一波派工前都檢查檔案和共用資源的歸屬。
- **完成是有限的。** Completion Contract 列出要證明的事和證據；全部通過後只跑預定的最後驗證。

完整內容見 [`native/CLAUDE.native.md`](native/CLAUDE.native.md)。

## 角色

| 角色 | 模型 | Effort | 工具 | 用途 |
|---|---|---|---|---|
| `Explore` | haiku | high | Read, Glob, Grep | 唯讀偵查 |
| `mech-executor` | haiku | high | + Write, Edit, Bash | 完全指定的機械性批次修改 |
| `test-analyst` | haiku | medium | Read, Glob, Grep, Bash | 跑既有測試、回歸分析 |
| `implementer-sonnet` | sonnet | high | + Write, Edit, Bash | 預設 implementer：方法已定的有界 slice（TDD） |
| `implementer` | opus | medium | + Write, Edit, Bash | 有設計選擇的 slice；困難的以 `high` 派 |
| `general-purpose` | opus | medium | Agent 以外全部 | 其他角色都不適合的有界非程式工作 |
| `code-reviewer` | fable | high | Read, Glob, Grep | 風險觸發的全新 context 審查 |
| `architecture-reviewer` | fable | high | Read, Glob, Grep | 對外介面或邊界改變時的審查 |

reviewer 只有在明確遇到 Fable 額度用完的錯誤時，才改用 `opus`。

## Handoff runtime

runtime 在 `~/.claude/provost/handoffs` 為每個 session 保存一條 sealed 的 handoff 紀錄鏈（來源、決策、派工、證據、下一步），主 checkout 和它的 worktree 共用。hook 會在壓縮或 resume 後注入最新 checkpoint、收集事實（改過的路徑、派工、commit——絕不存指令文字或檔案內容），並在派工或改檔還沒有紀錄時提醒主 session。指令：`doctor`、`status`、`record`、`snapshot`、`list`、`resume`、`changes`、`search`、`prune`。詳見 [`docs/native-handoff.md`](docs/native-handoff.md)。

## 安裝

需求：Claude Code、Python 3.11 以上；Windows 需要 Git Bash（hook 是 bash 腳本）。

1. 在 `~/.claude/CLAUDE.md` 想放政策的位置加上標記。安裝程式只會替換兩行之間的內容：

   ```markdown
   <!-- claudex-native:begin -->
   <!-- claudex-native:end -->
   ```

2. 預覽、安裝、檢查：

   ```bash
   python install.py check              # 唯讀；exit 0 = 無漂移，1 = 有漂移
   python install.py install --dry-run  # 列出會改什麼
   python install.py install            # 每個要改的檔案都先備份
   ```

   備份在 `~/.claude/provost/backups/install-<UTC 時間>/`，附 `manifest.json`；安裝失敗會自動回滾。支援 `CLAUDE_CONFIG_DIR` 和 `--config-dir`。

3. 在 `~/.claude/settings.json` 註冊 hook（安裝程式不會改這個檔；`check` 會回報有沒有註冊）。設定內容見 [README.md 的 Install 第 3 步](README.md#install)，`<runtime>` 換成 `~/.claude/provost/runtime/native-pilot-v1` 的絕對路徑。

4. 在 `~/.claude/provost/config.json` 啟用 runtime（範例見 [README.md 的 Install 第 4 步](README.md#install)），再用 `bash <runtime>/hooks/runtime doctor` 確認。

之後 repo 有任何變動，先跑 `python install.py check` 看和實際設定差在哪，再用 `install` 套用。

## 目前的證據

單一使用者、真實工作，不是受控實驗：

- `implementer-sonnet` 在 5 個 repo、34 個 slice 上和 `implementer` 對照試驗（含同一份 brief 的配對），2026-10-04 起成為預設 implementer。
- 兩週內（2026-09-28 到 10-12，84 個 session），主 session 只寫了約 3% 的程式碼行數，其餘由 implementer 角色透過 270 次派工完成。

## 測試

```bash
python -m unittest discover -s tests/adaptive -p "test_*.py"
```

CI 在 Windows、Ubuntu、macOS 上跑這套測試，並檢查 Markdown 連結。

## 歷史

Provost 最早是三層治理框架（Tier 0 Bare、Tier 1 Collaborator、Tier 2 Governed：hash 綁定的 manifest 與 PowerShell 寫入檢查），以 Claude Code plugin 發佈。那套設計封存在 tag [`archive/tier2-foreman`](https://github.com/norton77930/Provost/tree/archive/tier2-foreman)，不再維護。

## 已知限制

- 為單一使用者調校；模型別名（`fable`、`opus`、`sonnet`、`haiku`）實際對應哪個模型取決於你的 Claude Code 版本。
- runtime 裡還留著沒在用的 plugin 模式 SessionStart 路徑，`runtime_hash` 也還列著已移除的 `native-reminder` hook，兩者都排定移除。
- `legacy.py`（v1 store 的唯讀橋接）排定移除。
- hook 每次都會啟動 bash 和 Python；在 Windows 上每個 hook 約花一秒。

## 授權

[MIT](LICENSE)。第三方 skill 的出處見 [`skills/NOTICE.md`](skills/NOTICE.md)。
