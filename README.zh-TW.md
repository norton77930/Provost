# Provost

**Match oversight to blast radius.**（依風險調節監督強度）— 給 Claude Code agent 的漸進式治理框架。

*[English →](README.md)*

---

多數做法只給 AI 編碼代理**一種固定的流程**——改一個 typo 和做一次付款系統遷移,用的是同一套儀式。Provost 把**「監督」變成一把可調的 dial**。三層讓你依一次改動的 *blast radius*(能造成多大破壞、多難復原)調節儀式:一行修正單飛;一般功能走「模型分層的協作者團隊 + 單一 writer 紀律」;高風險改動則走「不可變 manifest + 引擎強制 write-scope + path custody + 逐項證據才算完成」。

它是 **model-agnostic** 的,跑在**原生 Claude Code** 上,用原生 Anthropic 模型——便宜模型做粗活,最強的模型只留給指揮與 review。

## 這把 dial

| 層 | 是什麼 | 何時用 |
|---|---|---|
| **0 · Bare** | 只有 Claude Code + 基本護欄,無團隊、無儀式。 | 拋棄式小改、問答、探索。 |
| **1 · 協作者** | 一支模型分層的 subagent 團隊、單一 active writer、有限的 Completion Contract。 | 一般功能與 bug fix。 |
| **2 · 治理** | 不可變 manifest、引擎強制 write-scope、custody、逐項證據、fresh verifier。 | auth、遷移、公開 API——任何你會被稽核的事。 |

重點**不是**永遠用第 2 層,而是**別再為第 0 層的工作付第 2 層的成本**——也別用第 0 層的隨性去做第 2 層的改動。完整模型與決策規則見 [`docs/concepts.md`](docs/concepts.md)。

## 快速開始(第 1 層——協作者團隊)

跑在原生 Claude Code 上;不需要 proxy、不需要額外服務。

1. 把角色檔複製進你的專案(或 `~/.claude/`):
   ```bash
   cp -r .claude/agents/ your-project/.claude/agents/
   ```
2. 把 [`orchestration.md`](orchestration.md) 併進你專案的 `CLAUDE.md`(或 `~/.claude/CLAUDE.md`)。
3. 用 Opus 跑 Claude Code,照常工作——先計畫,再讓團隊執行。主代理把唯讀工作委派給 `explorer` 與 reviewer、維持單一 active writer,並在 Completion Contract 達成時停手。

角色以釘死的模型讓成本對上任務:

| 角色 | 模型 | |
|---|---|---|
| `explorer` | haiku | 唯讀蒐證 |
| `implementer` / `implementer-deep` | sonnet / opus | 有界的 TDD writer |
| `test-analyst` | haiku | 只跑既有測試,絕不改檔 |
| `code-reviewer` / `architecture-reviewer` | opus | 唯讀 review |

> _Demo:terminal 錄影製作中——Opus 指揮 Haiku/Sonnet 分工跑一個真實任務。_

## 第 2 層——治理

高 blast radius 的工作,協作紀律要被**引擎強制**,而不只是口頭同意:不可變 manifest、在引擎層擋掉越界寫入的 hooks、path custody、逐項證據,以及一個必須通過才算完成的 fresh-context verifier。設計與 reference 實作:[`docs/governance/`](docs/governance/)。

## 接其他模型

Provost 就是提示詞、角色定義與一套治理設計——沒有任何一部分綁死某家模型或廠商。範例用原生 Anthropic 模型,任何人都能重現。想在 Claude Code 後面跑其他模型?用 [CC Switch](https://github.com/farion1231/cc-switch) 或 CLIProxyAPI 之類的 router,把它指向任何 Anthropic 相容 gateway 即可——那是外部選擇,Provost 不 ship、也不背書。Provost 是團隊與流程;router 只是決定他們跑在哪個引擎上。

## 這裡還有

- [`docs/field-notes.md`](docs/field-notes.md) — 把 Claude Code 操到極限的踩雷筆記(context window 內部行為;會一則訊息撐爆你視窗的內建 `claude-api` skill)。
- [`skills/`](skills/) — 團隊用的方法論 skills(TDD、bug 診斷、review、完成前驗證)。部分 vendored 自第三方 MIT 專案,見 [`skills/NOTICE.md`](skills/NOTICE.md)。

## 相關工作與定位

Provost 完全建在原生 Claude Code primitives 上——subagents、hooks、skills。它是一個**主動式**治理層:事前約束 scope、以證據 gate 完成。如果你要的是**事後可觀測性**——每個 session 的可查詢紀錄——像 [claude-agent-team](https://github.com/ek33450505/claude-agent-team) 這類專案是從「紀錄」的角度切入同一個「Claude Code governance layer」品類。

## 路線圖

- **Phase A(現在)**:協作層做成可跑、跨平台的 drop-in;治理層以設計 + 一份 Windows/PowerShell reference 實作呈現。
- **Phase B**:把治理層做成乾淨、跨平台、model-agnostic 的可安裝工具;可能加一個組角色目錄的 UI。

## 授權

MIT — 見 [`LICENSE`](LICENSE)。Vendored skills 保留其原始 MIT 歸屬於 [`skills/NOTICE.md`](skills/NOTICE.md)。
