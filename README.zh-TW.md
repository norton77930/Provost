# Provost

**Match oversight to blast radius.**（依風險調節監督強度）— coding-agent 工作的分級治理框架。

*[English →](README.md)*

Provost 用來判斷一次 AI coding 任務需要多少監督，並讓高風險工作更可問責。修 typo 不需要和 auth 改動、migration 或 public API 變更一樣的流程；Provost 提供三個 tier，讓治理成本跟著可能造成的損害上升。

治理模型是 model-agnostic；目前的 reference host 與 integrations 以 **Claude Code** 為目標，交付的 role files 也使用 Claude model identifiers。更廣的 agent-host 相容性是規劃方向，不是現在已可用的能力。

## 為什麼需要 Provost

Prompt 指示很有用，但 agent 可能誤解或忽略。對高 blast-radius 工作，Provost 探索更強的控制：把核准計畫釘在 manifest、依允許清單檢查檔案寫入、在交接時保留 path custody，並讓完成狀態經過宣告好的 verifier tasks。

因此 Provost 不只是一組 prompts。Tier 2 reference code 包含 lifecycle state、manifest hashing、write-gate decisions、workspace snapshots、custody records、failure signatures 與 audit ledger。由於 repository 尚未交付原始 launcher 或 turnkey installer，目前應把它視為 reference implementation。

## 三級治理

| Tier | 是什麼 | 適用情境 |
|---|---|---|
| **0 · Bare** | Claude Code 加上本機 guardrails；沒有 crew 或 governance runtime。 | 拋棄式小改、問答與探索。 |
| **1 · Collaborator** | 一組協作 roles、單一 active writer policy 與有限的 Completion Contract。 | 日常 feature 與 bug fix。 |
| **2 · Governed** | Hash-pinned manifest、hook-enforced write scope、path custody、verifier tasks 與 audit trail。 | Auth、secret、payment、migration、public API 等高風險工作。 |

```mermaid
flowchart TD
    C(["一個要做的改動"]) --> Q1{"拋棄式小改、<br/>問答或探索？"}
    Q1 -->|是| T0["Tier 0 · Bare<br/>Claude Code + guardrails"]
    Q1 -->|否| Q2{"auth · secret · payment ·<br/>migration · public API ·<br/>或需要 audit trail？"}
    Q2 -->|是| T2["Tier 2 · Governed<br/>hash-pinned manifest ·<br/>enforced scope · verifier gate"]
    Q2 -->|否| T1["Tier 1 · Collaborator<br/>model-tiered crew · single writer"]
```

完整決策規則與各 tier 邊界見 [`docs/concepts.md`](docs/concepts.md)。

## 專案狀態

### 現在可用

- [`.claude/agents/`](.claude/agents/) 下的六個 Claude Code role definitions。
- Tier 1 [`orchestration.md`](orchestration.md)：plan-first、單一 active writer 與 evidence-based completion discipline。
- [`skills/`](skills/) 中的 TDD、診斷、review 與完成前驗證方法；第三方歸屬保留於 [`skills/NOTICE.md`](skills/NOTICE.md)。
- Windows 上的 Tier 2 write-gate、ref-guard、manifest-pin 與 path-custody decision test；見 [governed write-scope demo](docs/examples/governed-write-scope-demo.md)、[governed ref-guard demo](docs/examples/governed-ref-guard-demo.md)、[governed manifest-pin demo](docs/examples/governed-manifest-pin-demo.md) 與 [governed path-custody demo](docs/examples/governed-path-custody-demo.md)。

### Experimental／reference implementation

- [`docs/governance/reference/`](docs/governance/reference/) 中的 Windows/PowerShell Tier 2 lifecycle helper 與 Claude Code hook scripts。
- Hash checks 能偵測已核准 manifest 被變更；變更 intent 必須建立新 revision，但作業系統並沒有把檔案設成不可修改。
- 當 hook 已安裝且 governed environment 啟用時，`PreToolUse` write gate 會對不在 running task literal `write_set` 內的 `Edit`、`Write` 或 `NotebookEdit` target 回傳 machine-readable deny。
- Helper 實作 workspace snapshots、path-custody hashes、task state、failure-signature handling、verifier-task completion gates、由 helper append 的 JSONL events，以及 hashed terminal handoff receipts。

### 尚未交付

- 原始 launcher 與可直接安裝的 Claude Code hook configuration。
- 乾淨的 end-to-end governed-session quickstart 或 packaged runtime。
- 完整逐 claim evidence binding。目前 acceptance entries 會被驗證，task 也可以記錄整體 verification summary，但 helper 不要求每個 acceptance claim 都有獨立 evidence record。
- Linux/macOS support，以及其他 coding-agent environment adapters。

精確能力邊界與下一步見 [governance capability matrix](docs/governance/README.md) 與 [`ROADMAP.md`](ROADMAP.md)。

## 如何試用

### Tier 1 collaborator workflow

Collaborator workflow 使用原生 Claude Code，不需要 proxy 或額外 service：

1. 把角色檔複製到專案或使用者層級的 `.claude` directory：

   ```bash
   cp -r .claude/agents/ your-project/.claude/agents/
   ```

2. 把 [`orchestration.md`](orchestration.md) 合併到專案的 `CLAUDE.md`。
3. 先建立 plan 與有限的 Completion Contract；需要時委派唯讀探索與 review，並維持單一 active writer。

目前交付的 role mapping：

| Role | 現有 model ID | 職責 |
|---|---|---|
| `explorer` | haiku | 唯讀 evidence gathering |
| `implementer` / `implementer-deep` | sonnet / opus | 有界的 TDD implementation |
| `test-analyst` | haiku | 不改 source 的既有測試執行 |
| `code-reviewer` / `architecture-reviewer` | opus | 唯讀 assurance |

這些是設定選擇，不是 portable model abstractions。你可以依 Claude Code environment 可用的 model 調整 mapping；Provost 不交付或認證第三方 gateway。

### Methodology skills

Claude Code **不會**自動載入 repo 根目錄的 [`skills/`](skills/)。把各 skill 目錄（不要複製 [`NOTICE.md`](skills/NOTICE.md)）拷到專案或使用者層級的 `.claude/skills/`：

```bash
cp -r skills/tdd skills/diagnosing-bugs skills/grilling skills/grill-me \
  skills/two-axis-review skills/verification-before-completion \
  your-project/.claude/skills/
```

Vendored skills 的歸屬見 [`skills/NOTICE.md`](skills/NOTICE.md)。

### Tier 2 write-scope decision

在 Windows 執行：

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-WriteScope.ps1
```

測試會建立隔離的 manifest 與 active-run lock，透過 stdin protocol 呼叫 repository 內的 hook，確認核准路徑被允許，並確認 scope 外或無效 state 的寫入被拒絕。它不會修改 repository。Prerequisites、預期輸出與限制見 [demo walkthrough](docs/examples/governed-write-scope-demo.md)。

### Tier 2 ref-guard decision

在 Windows 執行：

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-RefGuard.ps1
```

測試會把暫存目錄當成宣告的外部 read root，確認寫入類命令被拒絕、窄型讀取被允許、無法分類的命令改為 ask。它不會修改 repository。見 [demo walkthrough](docs/examples/governed-ref-guard-demo.md)。

### Tier 2 manifest-pin decision

在 Windows 且 `git` 可用時執行：

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-ManifestPin.ps1
```

測試會在隔離的 Git workspace 初始化最小 v1 manifest、通過 Validate，並確認被改過的 Plan 或已核准 manifest 會被拒絕。它不會修改 repository。見 [demo walkthrough](docs/examples/governed-manifest-pin-demo.md)。

### Tier 2 path-custody decision

在 Windows 且 `git` 可用時執行：

```powershell
powershell.exe -NoProfile -File .\tests\governance\Test-PathCustody.ps1
```

測試會拒絕共用 path 但沒有依賴順序的兩個 writer，並在有依賴時記錄、交接 custody。它不會修改 repository。見 [demo walkthrough](docs/examples/governed-path-custody-demo.md)。

## Tier 2 治理細節

Governed tier 的 reference design 包含：

- **Hash-pinned manifests：** lifecycle 會檢查核准內容的 hash；scope 變更需要新 revision。
- **Write-scope decisions：** write hook fail closed，拒絕 running task literal `write_set` 外的 target。
- **Path custody：** 共用 writer path 需要 dependency ordering，交接時記錄 content evidence。
- **Completion gating：** 成功完成要求所有宣告的 tasks（包含 required verifier tasks）都達到 `PASS`。
- **Failure control：** 重複的 failure signature 可要求新的 diagnosis evidence 才能繼續下一個 revision。
- **Audit artifacts：** lifecycle actions append JSONL events；terminal handoff receipt 以 hash 釘住 manifest、ledger 與 workspace snapshot。

只有 hooks 已註冊到 Claude Code，且 launcher 設好必要的 `PROVOST_*` environment 時，這些 controls 才會成為 engine-enforced 行為。目前 public repository 尚未包含 launcher/configuration。評估或移植前請先讀 [governance documentation](docs/governance/README.md)。

## 其他模型

治理概念不依賴特定模型的 reasoning style，但目前交付的 integration 依賴 Claude Code interfaces。若你自行在 Anthropic-compatible gateway 後方使用其他模型，gateway 是外部 setup；Provost 不交付也不背書。相關實務觀察另記於 [field notes](docs/field-notes.md)。

## 相關工作與定位

Provost 建在 Claude Code 的 subagents、hooks 與 skills 上。Collaborator pattern 也有其他成熟方向：

- [pilotfish](https://github.com/Nanako0129/pilotfish) 聚焦 frontier orchestrator 與較便宜 workers 的成本拆分。
- [claude-agent-team](https://github.com/ek33450505/claude-agent-team) 聚焦 local session observability。

Provost 聚焦 graduated governance：依 blast radius 提高監督強度，並在最高 tier 探索可強制的 scope、custody、verification 與 auditability。

## 貢獻

歡迎 bug reports、governance proposals、文件、測試、packaging 與 cross-platform work。請先讀 [`CONTRIBUTING.md`](CONTRIBUTING.md)，保持 PR scope 精簡，且不得無聲削弱已宣告的治理保證。顯著變更見 [`CHANGELOG.md`](CHANGELOG.md)。

## 授權

MIT — 見 [`LICENSE`](LICENSE)。Vendored skills 的原始 MIT 歸屬保留於 [`skills/NOTICE.md`](skills/NOTICE.md)。
