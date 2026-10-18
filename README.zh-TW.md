# Provost

**流程輕重跟著任務走。** —— Claude Code 的編排政策、角色組與 handoff runtime。

*[English →](README.md)*

<!-- 宣傳影片（zh-TW，46 秒） -->
https://github.com/user-attachments/assets/bd4974d6-1f6c-481e-8c40-2c687c8536b4

Provost 是一套每天實際在用的 Claude Code 設定的正本。主 session 先依難度和風險為每個請求分類：簡單的工作自己做，有界的 slice 交給比較便宜的 implementer 角色，只有在失敗「測試和實機都看不出來」時才找全新 context 的 reviewer。一個小型 runtime 會記下決策和派工，讓工作撐過 context 壓縮和 session 重啟。

它照實際使用的樣子公開：為一個人的工作調校、以 Windows 為主，用「複製檔案到 Claude 設定目錄＋漂移檢查」的方式安裝。

## 為什麼

- **問題：** main agent 什麼都自己做——改檔、跑測試、自我審查、開瀏覽器檢查——每一步都是 Opus 的價格。
- **① 先分類，再派工和驗收。** 簡單的工作留在主 session；機械性批次交給 Haiku 上的 `mech-executor`；UI 改動用實機瀏覽器檢查收尾；只有風險需要時才派 reviewer（例如錯誤處理路徑交給 `code-reviewer`）。
- **② Hook：早點壓縮，決策不會丟。** 每次呼叫的成本在 context 從 ≤200K 長到 400–600K 之間約增加 ×2.3。PreCompact 存下決策，PostCompact 還原，所以早點壓縮是安全的。
- **結果（一位使用者，真實工作）：** 用 `implementer-sonnet` 實作成本 −53~66%，品質相當（34 個 slice 的對照試驗）；主 session 每週成本 −37%（前後對照），Hook 本身只占總花費 3.6%（7 天實測）。

## 快速開始

```bash
git clone https://github.com/norton77930/Provost
python Provost/install.py setup
```

需求、`setup` 會改動什麼，以及如何手動完成，見[安裝](#安裝)。

## Repo 內容

| 路徑 | 內容 | 安裝到（`~/.claude` 底下） |
|---|---|---|
| [`native/CLAUDE.native.md`](native/CLAUDE.native.md) | 編排政策 | `CLAUDE.md` 中 `claudex-native` 標記之間 |
| [`agents/`](agents/) | 8 個 subagent 角色 | `agents/` |
| [`hooks/`](hooks/)、[`scripts/provost_runtime/`](scripts/provost_runtime/) | handoff runtime 與 hook | `provost/runtime/native-pilot-v1/` |
| [`skills/verification-before-completion/`](skills/verification-before-completion/) | 宣告完成前的驗證紀律 | `skills/` |
| [`skills/handoff/`](skills/handoff/) | `/handoff` skill（存檔、接續、搜尋） | `skills/handoff/` |
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

runtime 在 `~/.claude/provost/handoffs` 為每個 session 保存一條 sealed 的 handoff 紀錄鏈（來源、決策、派工、證據、下一步），主 checkout 和它的 worktree 共用。hook 會在壓縮或 resume 後注入最新 checkpoint、收集事實（改過的路徑、派工、commit——絕不存指令文字或檔案內容），並在派工或改檔還沒有紀錄時提醒主 session。決策與證據寫入每個工作一份、整個 repo 所有 session 共用的 append-only 紀錄。指令：`doctor`、`status`、`record`、`snapshot`、`list`、`resume`、`changes`、`search`、`prune`、`migrate`，以及只限維護者執行的 `redact`。詳見 [`docs/native-handoff.md`](docs/native-handoff.md)。

## 安裝

需求：Claude Code、Python 3.11 以上；Windows 需要 Git Bash（hook 是 bash 腳本）。

```bash
git clone https://github.com/norton77930/Provost
python Provost/install.py setup
```

接著重啟 Claude Code，用 `bash ~/.claude/provost/runtime/native-pilot-v1/hooks/runtime doctor` 確認。

`setup` 一次做完安裝需要的所有步驟，加 `--dry-run` 可以先看它會做什麼：

- 把政策、角色、skill 和 runtime 複製到 `~/.claude`（和 `install` 相同）；
- `~/.claude/CLAUDE.md` 沒有標記時，在檔尾補上 `<!-- claudex-native:begin -->` / `<!-- claudex-native:end -->`（政策只會寫在兩行之間；標記格式有誤會直接拒絕）；
- `~/.claude/settings.json` 缺少的 hook 註冊會補上 6 條（內容見 [README.md 的 Install](README.md#install)）。檔案裡其他內容都保留，重新以 2 格縮排寫回；不是合法 JSON 的檔案會拒絕處理，也絕不印出檔案裡的任何值；
- `~/.claude/provost/config.json` 不存在時，寫入開啟 handoff 的預設值。

每個要改的檔案都先備份到 `~/.claude/provost/backups/install-<UTC 時間>/`，附 `manifest.json`；失敗會自動回滾。重跑 `setup` 不會再改任何東西。第一次覆蓋內容不同的本機 `skills/handoff/SKILL.md` 時，會先在旁邊另存一份 `SKILL.md.local-<UTC 時間>.bak`。`settings.json` 的備份是完整副本（含其中的憑證），備份不會自動清除。安裝程式支援 `CLAUDE_CONFIG_DIR` 和 `--config-dir`；hook 本身讀的是 `~/.claude/provost/config.json`，除非用 `PROVOST_CONFIG` 指到別處。

之後 repo 有任何變動，先跑 `python install.py check` 看和實際設定差在哪（exit 0 = 無漂移，1 = 有漂移），再用 `python install.py install` 套用；`install` 不會碰 `settings.json`。升級到 decision log 後，先跑 `migrate` 並檢視結果，再跑 `migrate --apply`，最後才 `prune --apply`（見指南的 [Install and rollout](docs/native-handoff.md#install-and-rollout)）。

## 目前的證據

單一使用者、真實工作，不是受控實驗：

- `implementer-sonnet` 在 5 個 repo、34 個 slice 上和 `implementer` 對照試驗（含同一份 brief 的配對），2026-10-04 起成為預設 implementer。
- 兩週內（2026-09-28 到 10-12，84 個 session），主 session 只寫了約 3% 的程式碼行數，其餘由 implementer 角色透過 270 次派工完成。
- 在試驗過的 slice 上，`implementer-sonnet` 的成本比 `implementer` 低 53–66%，審查結果相當。
- 每次呼叫的成本從 ≤200K token 的 context 到 400–600K 約增加 ×2.3。提早壓縮（`autoCompactWindow`）讓主 session 的每週成本下降 37%（7 天實測）；handoff hook 是讓提早壓縮安全的關鍵，所以 −37% 是「設定加 hook」的合計效果，不是單靠 hook。
- 44 次壓縮中，沒有任何一個 session 需要使用者重新說明遺失的 context。
- 實測一週內，hook 機制本身只占總花費的 3.6%。

## 測試

```bash
python -m unittest discover -s tests/adaptive -p "test_*.py"
```

CI 在 Windows、Ubuntu、macOS 上跑這套測試，並檢查 Markdown 連結。

## 歷史

Provost 最早是三層治理框架（Tier 0 Bare、Tier 1 Collaborator、Tier 2 Governed：hash 綁定的 manifest 與 PowerShell 寫入檢查），以 Claude Code plugin 發佈。那套設計封存在 tag [`archive/tier2-foreman`](https://github.com/norton77930/Provost/tree/archive/tier2-foreman)，不再維護。

## 已知限制

- 為單一使用者調校；模型別名（`fable`、`opus`、`sonnet`、`haiku`）實際對應哪個模型取決於你的 Claude Code 版本。
- hook 每次都會啟動 bash 和 Python；在 Windows 上每個 hook 約花一秒。

## 授權

[MIT](LICENSE)。第三方 skill 的出處見 [`skills/NOTICE.md`](skills/NOTICE.md)。
