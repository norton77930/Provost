<p align="center">
  <img src="docs/logo.svg" width="64" height="64" alt="Provost logo">
</p>

<h1 align="center">Provost</h1>

<h3 align="center">不需要 Opus 的工作，就別付 Opus 的價格。</h3>

<p align="center">
  Provost 讓 Claude Code 把每個請求交給做得到的最便宜模型，<br>
  用證據而不是「看起來好了」來結案，並讓決策撐過 context 壓縮。
</p>

<table align="center">
  <tr>
    <td align="center" width="220"><h2>−34%</h2>整體花費<br><sub>10 天的真實工作</sub></td>
    <td align="center" width="220"><h2>−68%</h2>subagent 成本<br><sub>對比全部用 Opus 跑</sub></td>
    <td align="center" width="220"><h2>0</h2>次需要你重講<br><sub>44 次壓縮之後</sub></td>
  </tr>
</table>

<p align="center"><sub>單一使用者的實測，不是受控實驗。期間、對照基準和限制見<a href="#證據">證據</a>。</sub></p>

<p align="center">
  <a href="https://github.com/norton77930/Provost/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/norton77930/Provost/ci.yml?branch=main&style=flat-square&labelColor=16181d&label=CI" alt="CI"></a>
  <img src="https://img.shields.io/badge/works%20with-Claude%20Code-2ea44f?style=flat-square&labelColor=16181d" alt="支援 Claude Code">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ea44f?style=flat-square&labelColor=16181d" alt="MIT 授權"></a>
</p>

<p align="center">
  <a href="#安裝"><b>安裝</b></a> · <a href="#先試-mods">先試 mods</a> · <a href="#運作方式">運作方式</a> · <a href="#證據">證據</a> · <a href="README.md">English</a>
</p>

<p align="center">
  <img src="mods/board/docs/board-pane.png" width="760"
    alt="真實 session 裡的 board 面板：5 個計畫步驟完成 3 個，第 3 步底下有一次 implementer-sonnet 派工，第 4 步進行中；下方是 board round 1 的驗收 gate，tests 和 validate 已通過，實機檢查待做，merge 等使用者核可">
</p>

<p align="center"><sub>裝了 Provost 和 <a href="mods/board/">board</a> mod 的真實 session：計畫、每一步交給誰，以及還有什麼要證明才算做完。</sub></p>

## 它做的三件事

**1. 動手的是便宜的模型。** 主 session 在動手前先為每個請求分類。12 個檔案的改名交給
Haiku，有界的功能交給 Sonnet，一行的修正留在原地。兩週內主 session 只寫了約 3% 的程式碼，
其餘由比較便宜的角色透過 270 次派工完成。

**2. 「做完」要有證據。** 每個主張都用「最便宜、但真的抓得到失敗」的證據結案：測試、在瀏覽器或
CLI 實機跑一次，或在兩者都看不出來時派全新 context 的 reviewer。驗收 gate 在動工前就寫好，
gate 還沒過或失敗時，這一輪不能結束。動到認證、密鑰、金流或資料遷移的改動，會停下來等你決定。

**3. 決策撐過壓縮。** hook 在壓縮前存下決策、派工和證據，壓縮後再放回去，所以可以安心提早壓縮。
context 保持小，每次呼叫就保持便宜。

## 一個請求長什麼樣子

照平常的方式工作。動手之前，主 session 會先宣告它打算怎麼處理：

```text
> 把 12 個檔案的 getUser 改名成 fetchUser
分類:機械/無風險/證據:測試 → mech-executor            # 機械性、無風險、由測試收尾 → Haiku
> 幫註冊表單加上驗證
分類:中等/無風險/證據:實機(瀏覽器) → implementer-sonnet  # 中等、無風險、實機瀏覽器檢查 → Sonnet
> 處理 API client 的 token 過期
分類:中等/R1(錯誤處理)/證據:審查 → implementer-sonnet    # 錯誤處理實機看不出來 → 做完派 code-reviewer
> 在 users 表加一個 role 欄位
分類:困難/R2(migration)/證據:審查+架構 → 停,等你決定    # 資料遷移：先停下來問你
```

整個想法，46 秒看完：

https://github.com/user-attachments/assets/bd4974d6-1f6c-481e-8c40-2c687c8536b4

<p align="center"><sub>46 秒，中文字幕，只有配樂。英文版在 <a href="README.md">README.md</a>。</sub></p>

## 為什麼不直接用 Claude Code

一般的 Claude Code 是很強的 agent。它自己不會做的，是決定每一步由誰來做、用什麼證明做完了，
所以每一步都跑在主模型上，審查也只有在你想到要求時才會發生。

| 情況 | 一般的 Claude Code | 用 Provost |
| :--- | :--- | :--- |
| 改名牽涉 12 個檔案 | 主模型一個一個檔案改 | Haiku 上的 `mech-executor`，由測試收尾 |
| 有界的功能可以開始做 | 主模型自己寫 | `implementer-sonnet`：實作成本 −53~66%，審查結果相當 |
| 改動在畫面上看得到 | 程式碼看起來對就算完成 | 在瀏覽器、視窗或 CLI 實機跑過才算完成 |
| 錯誤處理、資料轉換、concurrency | 你要求才審查 | `code-reviewer` 在全新 context 審查，由風險觸發，不看 diff 大小 |
| 認證、密鑰、金流、migration | 和其他改動一樣直接進行 | 停下來等你決定 |
| context 超過 400K | 每次呼叫貴約 ×2.3，壓縮可能丟掉決策 | 提早壓縮，決策先存下再還原 |

<p align="center">
  <img src="docs/demo/hero-zh-TW.png" width="100%"
    alt="一樣的工作，不一樣的價格。左邊：一般的 Claude Code 每一步（改檔、測試、自我審查、瀏覽器、context 到 92%）都用 Opus。右邊：用 Provost，12 個檔案改名交給 Haiku 上的 mech-executor，按鈕文字留在 main，註冊表單交給 implementer-sonnet 並用實機瀏覽器檢查，token 過期錯誤處理交給 implementer-sonnet 再由 code-reviewer 審查，認證或 migration 停下來等你，context 接近上限時由 PreCompact 存檔、PostCompact 還原">
</p>

<p align="center"><sub>圖中的實作成本是 <code>implementer-sonnet</code> 對比 <code>implementer</code>，在 34 個試驗 slice 上的結果。整體花費下降 34%（見<a href="#證據">證據</a>）。</sub></p>

## 先試 mods

這個 repo 裡有兩個 mod，不用裝 Provost 的其他部分就能用，是最快感受它在做什麼的方式。

| Mod | 用途 |
|---|---|
| [`board`](mods/board/) | 用面板和狀態列顯示計畫步驟、subagent 派工和驗收 gate（就是上面那張截圖） |
| [`idle-guard`](mods/idle-guard/) | 對話閒置、context 又很大時，在一小時的 prompt cache 過期前先壓縮，晚點回來就不用重讀整個 context |

```text
/plugin marketplace add norton77930/Provost
/plugin install board@provost
/plugin install idle-guard@provost
```

介面預設是英文。要改成繁體中文，在 `/config` 把該 mod 的 `language` 設為 `zh-TW`。

## 安裝

需求：Claude Code、Python 3.11 以上；Windows 需要 Git Bash（hook 是 bash 腳本）。

```bash
git clone https://github.com/norton77930/Provost
python Provost/install.py setup
```

接著重啟 Claude Code，用 `bash ~/.claude/provost/runtime/native-pilot-v1/hooks/runtime doctor` 確認。

<details>
<summary><b>setup 會改動什麼、備份與升級</b></summary>

`setup` 一次做完安裝需要的所有步驟，加 `--dry-run` 可以先看它會做什麼：

- 把政策、角色、skill 和 runtime 複製到 `~/.claude`（和 `install` 相同）；
- `~/.claude/CLAUDE.md` 沒有標記時，在檔尾補上 `<!-- claudex-native:begin -->` / `<!-- claudex-native:end -->`（政策只會寫在兩行之間；標記格式有誤會直接拒絕）；
- `~/.claude/settings.json` 缺少的 hook 註冊會補上 8 條（內容見 [README.md 的 Install](README.md#install)）。檔案裡其他內容都保留，重新以 2 格縮排寫回；不是合法 JSON 的檔案會拒絕處理，也絕不印出檔案裡的任何值；
- `~/.claude/provost/config.json` 不存在時，寫入開啟 handoff 的預設值。

每個要改的檔案都先備份到 `~/.claude/provost/backups/install-<UTC 時間>/`，附 `manifest.json`；失敗會自動回滾。重跑 `setup` 不會再改任何東西。第一次覆蓋內容不同的本機 `skills/handoff/SKILL.md` 時，會先在旁邊另存一份 `SKILL.md.local-<UTC 時間>.bak`。`settings.json` 的備份是完整副本（含其中的憑證），備份不會自動清除。安裝程式支援 `CLAUDE_CONFIG_DIR` 和 `--config-dir`；hook 本身讀的是 `~/.claude/provost/config.json`，除非用 `PROVOST_CONFIG` 指到別處。

之後 repo 有任何變動，先跑 `python install.py check` 看和實際設定差在哪（exit 0 = 無漂移，1 = 有漂移），再用 `python install.py install` 套用；`install` 不會碰 `settings.json`。升級到 decision log 後，先跑 `migrate` 並檢視結果，再跑 `migrate --apply`，最後才 `prune --apply`（見指南的 [Install and rollout](docs/native-handoff.md#install-and-rollout)）。

</details>

## 運作方式

Provost 有三個部分：主 session 遵循的政策（[`native/CLAUDE.native.md`](native/CLAUDE.native.md)）、
9 個 subagent 角色，以及一組小型的 hook runtime。每個非瑣碎的請求先宣告一行分類：

```text
分類:<簡單|中等|機械|困難> / <無風險|R1|R2> / 證據:<測試|實機|審查|架構> → <路徑>
```

- **難度決定誰來實作。** 簡單的留在主 session；中等的交給 `implementer-sonnet`（方法已定、驗收清楚）或 `implementer`（還有設計選擇；concurrency、lifecycle、安全邏輯則用 `effort: high`）；完全指定的重複工作交給 `mech-executor`；困難的工作要先有核准的 spec。
- **風險決定用什麼證據結案。** 選「最便宜、但真的抓得到失敗」的證據：聚焦測試、實機執行，或只在兩者都看不出來時派 `code-reviewer`（動到對外介面再加 `architecture-reviewer`）。檔案數、diff 大小、「看起來很複雜」明文不是審查的理由。需要審查的改動就是 **R1**。
- **R2 先停。** 認證、密鑰、金流、migration、破壞性 public API、跨 repo 或不可逆操作，等使用者決定，並且要審查通過後才算完成。
- **驗收 gate 擋住這一輪。** 動工前，session 先把 gate（`machine`、`live`、`visual`、`review`、`human`）寫進 `.claude/acceptance/<task>.json`。非 human 的 gate 還沒過、失敗或過期時，Stop hook 會擋回去；visual gate 由 `acceptance-verifier` 對照設計稿判定，而不是由做這份工作的 session 自己判。
- **writer 有預算。** 平常最多 2 個同時寫入，第 3 個要證明完全獨立；每一波派工前都檢查檔案和共用資源的歸屬。
- **完成是有限的。** 所有主張都通過後，只跑預定的最後驗證，不再追加審查、測試或重構。

### 角色

| 角色 | 模型 | Effort | 工具 | 用途 |
|---|---|---|---|---|
| `Explore` | haiku | high | Read, Glob, Grep | 唯讀偵查 |
| `mech-executor` | haiku | high | + Write, Edit, Bash | 完全指定的機械性批次修改 |
| `test-analyst` | haiku | medium | Read, Glob, Grep, Bash | 跑既有測試、回歸分析 |
| `implementer-sonnet` | sonnet | high | + Write, Edit, Bash | 預設 implementer：方法已定的有界 slice（TDD） |
| `implementer` | opus | medium | + Write, Edit, Bash | 有設計選擇的 slice；困難的以 `high` 派 |
| `general-purpose` | sonnet | high | Agent 以外全部 | 其他角色都不適合的有界非程式工作 |
| `code-reviewer` | fable | high | Read, Glob, Grep | 風險觸發的全新 context 審查 |
| `acceptance-verifier` | sonnet | high | Read, Glob | 對照來源判定 visual、live 與門檻 gate，回報 PASS、ISSUES 或 BLOCKED |
| `architecture-reviewer` | fable | high | Read, Glob, Grep | 對外介面或邊界改變時的審查 |

reviewer 只有在明確遇到 Fable 額度用完的錯誤時，才改用 `opus`。

### Handoff runtime

runtime 在 `~/.claude/provost/handoffs` 為每個 session 保存一條 sealed 的 handoff 紀錄鏈（來源、決策、派工、證據、下一步），主 checkout 和它的 worktree 共用。hook 會在壓縮或 resume 後注入最新 checkpoint、收集事實（改過的路徑、派工、commit；絕不存指令文字或檔案內容），並在派工或改檔還沒有紀錄時提醒主 session。決策與證據寫入每個工作一份、整個 repo 所有 session 共用的 append-only 紀錄。指令：`doctor`、`status`、`record`、`snapshot`、`list`、`resume`、`changes`、`search`、`prune`、`migrate`，以及只限維護者執行的 `redact`。詳見 [`docs/native-handoff.md`](docs/native-handoff.md)。

### Repo 內容

| 路徑 | 內容 | 安裝到（`~/.claude` 底下） |
|---|---|---|
| [`native/CLAUDE.native.md`](native/CLAUDE.native.md) | 編排政策 | `CLAUDE.md` 中 `claudex-native` 標記之間 |
| [`agents/`](agents/) | 9 個 subagent 角色 | `agents/` |
| [`hooks/`](hooks/)、[`scripts/provost_runtime/`](scripts/provost_runtime/) | handoff runtime 與 hook | `provost/runtime/native-pilot-v1/` |
| [`skills/verification-before-completion/`](skills/verification-before-completion/) | 宣告完成前的驗證紀律 | `skills/` |
| [`skills/handoff/`](skills/handoff/) | `/handoff` skill（存檔、接續、搜尋） | `skills/handoff/` |
| [`tools/dispatch-report.js`](tools/dispatch-report.js) | 從 transcript 統計過往派工的成本與結果 | `experiments/` |
| [`mods/`](mods/) | `board` 和 `idle-guard` 兩個 plugin | 用 `/plugin` 安裝 |
| [`install.py`](install.py) | 安裝程式與唯讀漂移檢查 | — |

方法論 skill（TDD、除錯、grilling、spec 與 ticket）由 [`mattpocock-skills`](https://github.com/mattpocock/skills) plugin 提供，政策直接引用它們的名稱。

## 證據

單一使用者、真實工作，從 Claude Code 的 transcript 量測，不是受控實驗。

- **整體花費 −34% 到 −36%**：10 天（2026-10-14 到 10-23），對照同一批 transcript 改用預設壓縮視窗、每個 subagent 角色往上一級模型（Haiku 角色改 Sonnet，`implementer-sonnet` 改 Opus）的價格。審查占花費的 7.7%。
- **subagent −68%**：同樣 10 天，對照全部用 Opus 的基準。實作 −50%、探索 −88%、測試與批次 −97%。
- **主 session −45.6%**：同樣 10 天，提早壓縮的結果（$980，預設視窗則是 $1,802）。handoff hook 是讓提早壓縮安全的關鍵，所以這是「設定加 hook」的合計效果。
- **`implementer-sonnet` −53~66%**：在 5 個 repo、34 個 slice 上和 `implementer` 對照（含同一份 brief 的配對），審查結果相當。2026-10-04 起成為預設 implementer。
- **主 session 只寫了約 3%**：兩週內（2026-09-28 到 10-12，84 個 session）變更的程式碼行數，其餘由 implementer 角色透過 270 次派工完成。
- **不用重講**：44 次壓縮中，沒有任何一個 session 需要使用者重新說明遺失的 context。
- **審查都有理由**：`code-reviewer` 派工中 92% 有真正的風險觸發（同樣 10 天稽核，86 次中 79 次）。
- **額外成本**：hook 機制本身約占總花費的 3%。

## 已知限制

- 為一個人的日常工作調校，照實際使用的樣子公開：以 Windows 為主，用「複製檔案到 Claude 設定目錄＋漂移檢查」的方式安裝。
- 模型別名（`fable`、`opus`、`sonnet`、`haiku`）實際對應哪個模型，取決於你的 Claude Code 版本。
- hook 每次都會啟動 bash 和 Python；在 Windows 上每個 hook 約花一秒。

## 測試

```bash
python -m unittest discover -s tests/adaptive -p "test_*.py"
```

CI 在 Windows、Ubuntu、macOS 上跑這套測試、測試 mods，並檢查 Markdown 連結。

## 參與貢獻

請看 [CONTRIBUTING.md](CONTRIBUTING.md)。版本紀錄在 [CHANGELOG.md](CHANGELOG.md)，計畫在 [ROADMAP.md](ROADMAP.md)。想法和錯誤回報請開 [issue](https://github.com/norton77930/Provost/issues/new/choose)。資安問題請看 [SECURITY.md](SECURITY.md)。

Provost 最早是以 Claude Code plugin 發佈的三層治理框架，那套設計封存在 tag [`archive/tier2-foreman`](https://github.com/norton77930/Provost/tree/archive/tier2-foreman)，不再維護。

## 授權

[MIT](LICENSE)。Copyright (c) 2026 Norton。第三方 skill 的出處見 [`skills/NOTICE.md`](skills/NOTICE.md)。
