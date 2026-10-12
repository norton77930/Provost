# Claude Code 委派與 Handoff 設計草稿

> **已被取代（2026-10-05）。** 本文是 2026-09-21 的設計草稿，保留作為來源紀錄；「尚未實作」與「store 放在專案內」均已不成立。handoff 已實作，紀錄改存 `~/.claude/provost/handoffs/v2/repos/<repo key>/`，主 checkout 與其 worktree 共用，不需 Git exclude；舊的 `.provost/handoffs/v1` 只經 `legacy.py` 唯讀讀取。現行設計見 `docs/analysis/2026-10-05-handoff-store-relocation-and-retention.md`，操作程序見 `docs/native-handoff.md`。

日期：2026-09-21
狀態：主要設計方向已獲使用者確認；新增相容性分析供審閱。尚未實作，尚未核准實作計畫。

## 1. 目的與授權範圍

讓 main agent 專注需求釐清、技術判斷、委派與整合，降低不必要的等待及 context 負擔，同時優先減少返工。使用者目前以原生 Claude Code 為主，日常流程不再使用 Foreman-Manifest。

本輪授權為設計文件與相容性分析。本文不變更現行 agent、Hook、CLAUDE.md、個人設定或 Git 流程，也不移除既有 Tier 2 功能。

對話中的政策選擇不等於已部署功能。尤其本 repo 現行規則仍要求單一 writer；本文的多 writer 政策必須在後續獲准修改規則後才生效。建立 worktree、commit、branch、MR／PR 或發布不因本設計而取得授權。

## 2. 現況與設計邊界

Repository-local 依據：

- [README](../../README.md)：目前 Tier 1 提供角色、單一 writer 協調與完成證據；Tier 2 為較重的治理參考實作。
- [orchestration.md](https://github.com/norton77930/Provost/blob/archive/tier2-foreman/orchestration.md)（已封存）：目前協調規則；本文不是其替代檔。
- [grilling](https://github.com/norton77930/Provost/blob/archive/tier2-foreman/skills/grilling/SKILL.md)（已封存）：本 repo 採一題一題討論，使用者確認理解一致後才行動。

先前唯讀分析發現，使用者的個人 Claude 設定與公開 repo 的預設不同。本設計整理未來政策，不將個人實驗設定描述成公開 repo 已有能力。

不在本輪加入完整 manifest、任務資料庫、全面事件紀錄或自動清理系統。節省 Token 與縮短工時是待量測目標，不是已證實結果。

## 3. 已確認：需求流程與來源

### 3.1 相容不同流程

Provost 不綁定 spec-kit、OpenSpec 或 Matt Pocock skills。既有需求文件可繼續使用，新功能可以試行 Matt 流程；不要求全面遷移舊文件。

程式碼平台與任務來源分開設定。例如程式碼在 GitLab，任務仍可放本地 Markdown。支援目標包含 GitHub Issues／PR、GitLab Issues／MR、本地文件，以及專案明確指定的其他來源；其他來源的實際連接能力需另行確認。

來源選擇順序：

1. 使用者當次明確指定的文件、Issue 或交接。
2. 專案文件記錄的預設，由 CLAUDE.md 等入口引用。
3. 探索後仍缺失或有歧義，才詢問使用者。

穩定規則存於專案文件，不只依靠對話記憶。紀錄包含需求位置、任務位置、平台／工具與專案限制；不存憑證。Handoff 保存本次實際選用的來源。

此處「專案預設」指讀取專案既有記憶、文件與限制，不要求另建可分享的 Provost 設定。使用者的委派、模型與 Handoff 政策採個人全域設定；試行可在個人設定中限定指定專案。本地交接資料仍按專案保存，與政策的設定層級分開。

### 3.2 Matt skills 的候選使用流程

一般功能：

```text
grill-me 或 grill-with-docs
  → to-spec
  → 必要時 to-tickets
  → 使用者確認需求、驗收、任務拆分
  → main 排程與委派
  → implementer 實作、驗證
  → 必要審查、整合驗證、更新進度
```

小修正不強制產生完整文件組。大型且方向未明的工作才考慮 wayfinder。Handoff 可發生於討論、實作或驗證階段。

原版與本設計的差異：

- 目前上游 grilling 可每輪問多個獨立問題；保留使用者偏好的一題一題方式。
- CONTEXT.md 為術語表，ADR 記錄重要取捨；不拿它們當任務進度表。
- to-tickets 提供依賴與驗收，不代表已檢查檔案或執行資源衝突。
- implement 原版要求最後 commit；必須服從專案授權，不直接照做。
- code-review 原版主要使用基準點到 HEAD 的 diff；適配時須涵蓋本次未提交修改及新增檔案，避免漏審。
- 原版 handoff 是使用者呼叫的 skill，寫至 OS 暫存目錄；沒有明定歷史、過期、清理或自動恢復機制。
- 多個上游流程入口要求使用者呼叫；上述箭頭是工作流程，不表示安裝後自動串接所有 skills。

## 4. 已確認：執行自主權與委派

### 4.1 計畫確認後自主執行

使用者確認需求、驗收與任務拆分後，main 自主排程、委派、選擇模型、處理回報及範圍內修正，不逐次詢問。改變需求、擴大範圍或遇到需使用者裁決的取捨時回來討論。

main 可自行拆分或合併 brief、調整順序或改成序列執行，前提是需求、驗收、已授權範圍與重要設計均不變，並更新短紀錄。新增交付、刪減驗收或改變重要介面／設計則回到使用者確認。

個人全域政策與專案既有規則衝突時，先遵守專案限制、指出衝突來源並繼續可做的工作；取得明確例外或規則修改授權後，才放寬限制。全域的 writer 上限不默默覆蓋專案的單一 writer 規則。

### 4.2 委派原則

- 有明確邊界的實作預設委派。
- main 可處理極小修正、與診斷緊密相連的修正或整合工作，但應有具體原因。
- 問題尚未釐清時先診斷，不把模糊問題當成完整實作任務丟給 worker。
- reviewer 與 test analyst 保持唯讀；需要修改時交回負責 writer。

### 4.3 平行排程

未來預設最多兩個 writer；第三個任務確實獨立且已就緒時可擴至三個，由 main 說明原因。此數量是起始政策，不是經驗證的最優值。

此處數量與下節 Opus／high 優先為使用者試行政策；公開產品的預設值尚未定案，不因試行決策直接替換既有使用者設定。

每次派工同時檢查：

1. 需求與介面是否足夠明確，前置依賴是否已完成。
2. 修改範圍是否衝突，包括共用型別、註冊檔與生成檔。
3. 是否共用瀏覽器、port、資料、build 輸出或其他可變執行資源。
4. 結果如何整合與驗收，是否需要安排共同資源的使用順序。

無任務依賴不等於可以安全平行。實作可平行，驗證仍可能需序列化。共享 checkout 且範圍互不干擾為預設候選；必要時考慮隔離，成本不值得則序列執行。建立／移除 worktree 的授權與生命週期尚待決定。

main 在實際派工時補充當下的檔案責任範圍及共用資源，不要求長期 spec 寫死檔案清單。

### 4.4 模型、effort 與失敗升級

- main 的模型與 effort 由使用者自行選擇。
- implementer 預設 Opus／high，以減少返工為優先。
- 修改方法已決定、影響範圍明確、驗收清楚且無未解設計問題時，可派 Sonnet／high；不確定則用 Opus／high。
- 既有機械操作角色的 Sonnet／low 設定與一般實作政策分開，不將一般實作一律降為 low 或 medium。
- 每次派工時選擇模型，不假設已執行中的 agent 會跟著 main 切換。
- 正常 TDD 的預期失敗不算卡關。知道如何修正可繼續；需求或設計超出範圍立即交回 main。
- 一次有證據依據的修正仍遇同一阻礙，或沒有合理下一步時，回報證據、已嘗試方式與修改範圍。main 決定升級 Opus 或接手，不為單純模型升級再詢問使用者。

候選配置為角色預設 opus／high、派工時必要時覆寫 model 為 sonnet。官方支援範圍與環境變數優先序曾查核，但實際模型路由尚未在目標環境驗證；不得宣稱設定已生效，也不假設派工工具支援動態 effort 參數。

## 5. 已確認：Handoff 行為

### 5.1 目的與保存位置

先確保連續工作，再量測 context 與成本效益。交接資料放在專案內的本地目錄、不納入 Git，並保留歷次快照。確切目錄、格式及排除方式尚未定案。

本地歷史不會隨 clone／pull 傳送，也不是未提交程式碼的備份。Git commit 識別碼不能重建當時的 dirty working tree。

### 5.2 更新與快照

- main 在計畫獲准、派工、收到結果、遇到阻礙、需求／決策改變及調整執行分派時，更新正式文件及必要短紀錄。僅更新變動內容，不重寫全文，不逐次記錄工具操作。
- Hook 負責壓縮／恢復相關的機械保存與載入，不在每次工具呼叫後用模型重寫全文。
- 每次交接新增快照，舊快照不覆蓋。歷史按需讀取，不全部注入 context。
- 正式文件已記錄的內容只引用；短紀錄補充執行中工作、阻礙、未落到正式文件的決策及下一步。
- 需求取消、被取代等重要決策應及時落盤，不能只等下一次交接。

Hook 只能保存可取得的資料，不能保證 main 已完整記錄所有語義。缺失或落後的紀錄需標示待核對，不將保存成功等同交接完整。若程序異常終止，也不能承諾取得最後一刻的狀態。

### 5.3 最小交接資訊

下列為語義需求，欄位名稱與序列化格式尚待設計：

- 專案／工作區識別、工作主題、來源 session、時間及前一份交接。
- 目前階段、需求與任務來源。
- 工作分派及交接當時觀察到的 worker 狀態。
- 已確認決策、取消或替代事項的必要提示。
- 已驗證／未驗證項目、證據位置及其適用狀態。
- 阻礙、下一步，以及必要時建議使用的 skills。

不得只靠功能名稱或全專案最新時間選取交接。同名工作、不同 worktree、不同 session 可能有不同進度。

### 5.4 過期與已完成事項

| 情況 | 行為 |
|---|---|
| 同一交接鏈已有新版 | 舊版保留為歷史，不預設載入 |
| 任務完成 | 正式任務來源更新狀態；新交接不再列為待辦，必要時引用成果證據 |
| 任務取消或被取代 | 記錄原因與新來源，避免重新執行 |
| 程式碼、需求或環境改變 | 重新核對受影響結論，不能沿用不適用的測試通過紀錄 |
| 只是時間久了 | 不單憑日期判定錯誤或刪除 |

正式任務來源是主要進度紀錄，但不是完成證據。它與交接或工作區矛盾時，main 核對相關事實後修正紀錄；不盲信任一份文件。

目前不自動清理或刪除歷史。若日後需要容量管理，另行決策保留政策。

### 5.5 恢復規則

同一 session 壓縮後：自動取得自己的交接，核對相關現況後繼續。

新 session：使用者指定交接或明確要求接續才恢復。單純開啟專案不啟動舊任務。有多個合理候選才詢問。

恢復核對限於下一步相關資料，不每次重讀全 repo：

1. 確認交接屬於正確工作區與工作。
2. 核對目前需求、任務狀態及相關變更。
3. 確认 worker 是否仍在執行、結果能否取得；當時的 agent ID 不保證新 session 可以接回。
4. 自行查明缺口後繼續；涉及需求歧義、無法判斷的工作所有權或可能重複執行才詢問使用者。

無法讀取 GitHub／GitLab 等來源時，不把無法核對解讀成已確認；受影響工作是否可繼續取決於已有證據，必要時說明阻礙。

## 6. 候選 Hook 分工與可靠性要求

此節為待實作驗證的方案，不是已部署的設定。

- PreCompact：保存已有短紀錄與來源識別；不能假設它會讓 main 額外完成一次語義整理。
- PostCompact：可取得 Claude 產生的摘要，作補充或追查來源；不將其自動視為權威任務狀態。
- SessionStart 的 compact／resume 等情境：辨識所屬 session，依前述規則注入接續資訊。全新 session 不自動執行舊工作。
- 主動交接：如何連接使用者呼叫的 handoff skill 與共同保存邏輯，尚待設計。

普通程式需要保證：識別碼不衝突、同事件重試不重複建檔、完整快照成功後才更新指標、讀者不讀到半寫入內容，以及指標損壞時可找到前一份完整紀錄。

main 負責語義紀錄；Hook 負責快照。兩者不能同時改寫同一份內容而沒有一致性處理。worker 不直接更新共用交接主紀錄。具體保存協定留待實作計畫。

### 6.1 已確認：保存失敗與通知

- 保存失敗不阻擋壓縮；保留上一份完整快照，不更新最新指標為失敗版本。
- 同一 session、同一原因首次失敗，清楚通知本次交接未保存且壓縮仍繼續；再次失敗簡短顯示，不反覆長篇說明。
- 保存恢復成功時通知一次；恢復工作時，若使用舊快照則明確標示並核對缺口。
- 使用可用的通知管道，不保證寫入失敗時還能另存失敗標記。各事件可用的通知方式、去重狀態與故障偵測仍需實作驗證，不假設 PreCompact 的任意輸出都會呈現給使用者。

## 7. 待決定／待驗證

| 項目 | 目前邊界 |
|---|---|
| 個人試行與公開 repo 的落地順序 | 已決定先在 Provost 實作及驗證，再於 Chrome Extension 專案試行；本輪未授權實作或啟用 |
| 儲存格式與路徑 | 已決定專案本地、不進 Git；名稱、索引與 schema 未定 |
| 工作、session、worktree 的對應 | 必須避免誤接；具體識別方式未定 |
| 保存失敗處理 | 已決定不阻擋壓縮、保留完整舊快照及分級通知；通知管道、去重與重試細節待實作設計 |
| 紀錄新鮮度 | 已知存在語義遺漏窗口；偵測與補查方式需驗證 |
| worktree | 建立／移除授權及整合方式未定 |
| review 範圍 | 保留需求與工程標準兩軸；不預設每次都啟動兩位 reviewer |
| CLI 與模型 | Hook 順序、Windows 路徑、環境變數覆寫與實際路由需在目標環境確認 |
| 平台操作 | GitHub、GitLab、本地來源都要涵蓋；認證與公司環境存取尚未驗證 |
| 成本與速度 | 未有比較實驗；記錄返工、牆鐘時間、main context 與可取得的 usage，再評估 |

## 8. 後續驗收情境（尚未執行）

本次僅文件整理，未新增或執行測試。未來獲准行為變更後，依 repo 規則先建立聚焦的失敗檢查，再實作及回歸。

| 情境 | 預期結果 |
|---|---|
| 同 repo 兩個 session、同名功能不同 worktree | 不互相覆寫、不接錯交接 |
| 同一 session 壓縮 | 載入相符紀錄，核對後繼續 |
| 新 session 只是提問 | 不自動恢復或執行舊任務 |
| 新 session 要求接續但候選不唯一 | 詢問一次，確認正確工作 |
| 舊交接待辦已完成／取消 | 不重新派工，更新接續資訊 |
| 決策尚未落盤就壓縮 | 不宣稱紀錄完整；可追查並核對缺口，無法釐清才詢問 |
| 交接記錄 worker 執行中但現況不明 | 不直接建立可能重複的 writer |
| 寫入中斷、Hook 重試、指標損壞 | 不讀半份快照、不重複建檔，可退回完整紀錄 |
| GitHub／GitLab／本地任務來源 | 使用專案設定與當次指定，不寫死單一平台 |
| 遠端來源暫時不可讀 | 明示未核對狀態，不據此誤判完成 |
| 測試通過後相關程式再改動 | 不把舊證據當成目前版本的驗證 |
| 多個 ready 任務共用可變資源 | 識別衝突，序列化必要部分 |
| 模型預設與覆寫 | 實際觀察 Opus／high 及 Sonnet／high 路由，確認覆寫條件 |
| 審查前尚未 commit 且有新增檔案 | 審查涵蓋本次修改，不只讀 HEAD diff |

## 9. 外部參考與證據限制

- [Claude Code Hooks](https://code.claude.com/docs/en/hooks)：事件、輸入輸出及壓縮支援；官方文件會變動，實作前需依目標 CLI 重查。
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)：角色模型、effort 與覆寫規則；官方支援不等同本機已驗證。
- [Matt skills 查閱版本 c55ee46](https://github.com/mattpocock/skills/tree/c55ee46073ed923f86ce59a5eb3b6d895095d1b7)：本文引用的流程與原版差異依此版本分析。
- [Matt handoff](https://github.com/mattpocock/skills/blob/c55ee46073ed923f86ce59a5eb3b6d895095d1b7/skills/productivity/handoff/SKILL.md)：引用已有文件、暫存交接的來源；本設計的歷史與恢復規則屬新增設計。
- [Matt to-tickets](https://github.com/mattpocock/skills/blob/c55ee46073ed923f86ce59a5eb3b6d895095d1b7/skills/engineering/to-tickets/SKILL.md)：可驗收的切片及依賴。
- [Matt implement](https://github.com/mattpocock/skills/blob/c55ee46073ed923f86ce59a5eb3b6d895095d1b7/skills/engineering/implement/SKILL.md)／[code-review](https://github.com/mattpocock/skills/blob/c55ee46073ed923f86ce59a5eb3b6d895095d1b7/skills/engineering/code-review/SKILL.md)：需適配 commit 授權與未提交變更的審查範圍。
- [Pilotfish 參考版本](https://github.com/Nanako0129/pilotfish/tree/09d764dd03e91431e5e3a0611bffa5ea5dc0b571)：先前討論參考其依賴派工、委派成本與避免重讀的觀點；未採用其整套流程。

## 10. 審閱後的下一步

主要方向已確認。接下來審閱第 12 節的相容性結論，再逐項決定第 7 節中影響首版的細節，最後提出有範圍與驗收的實作計畫。本文不授權直接進入實作、安裝、commit 或發布。

## 11. 已確認：開源發布與試行邊界

- 新委派模式與 Handoff 由使用者明確啟用；既有使用者更新插件後不自動切換。採個人全域政策，試行可限定指定專案，不要求 repo 新增共享啟用設定。
- 在 Provost 完成實作及驗證後，再於使用者指定的 Chrome Extension 專案試行；具體工作區與啟用變更另行確認。
- 試行沿用該專案現有規格流程，不要求先轉用 Matt skills。
- 指定專案與發布方向不等於已通過測試或已啟用。穩定後才評估擴大採用及公開預設。

開源設計建議：共通規則處理來源、依賴、衝突、證據與恢復；模型及 writer 數量為可調政策。個人全域政策與不進 Git 的專案執行資料分開；其他使用者或公司自行依公開安裝文件設定，不替團隊配置環境。GitHub、GitLab 及 Matt skills 均不應成為本地文件模式的強制依賴。新增 runtime 須明示支援平台、Claude Code 版本及缺少能力時的行為；Windows 試行不代表跨平台驗證完成。

## 12. 相容性分析：spec-kit、OpenSpec、Matt skills

### 12.1 結論與證據層級

三者可以共用委派與交接的核心語義，但不能使用同一組固定路徑或 checkbox 規則處理全部流程。建議共用核心加薄的來源適配說明，先由 main 依據來源讀取與判斷；Hook 不負責理解任意規格格式。是否將特定解析做成程式，留待首版計畫決定，不先建立大型適配框架。

目前完成官方來源與本地文件的靜態分析，沒有端到端相容性測試，不能宣稱三者已通過支援認證。

| 流程 | 查閱依據 | 目前證據 |
|---|---|---|
| spec-kit | 上游 `730dcf53d04b4958e7013c7c448de11e9c8fa4a7`；試行專案的 scripts、skills 與 tasks | 有官方與真實專案文件；未執行流程 |
| OpenSpec | 上游 `bae58cf61479986431bb798acbe5a688a591c18c` 的 schema、apply、archive | 官方內容靜態分析；試行專案根目錄未發現 openspec/，不當作已有 OpenSpec 試行樣本 |
| Matt skills | 第 9 節固定版本的 to-spec、to-tickets、implement、handoff | 官方內容靜態分析；尚未使用該流程跑實際功能 |

版本不可混用：本機 `specify version` 回報 CLI 1.0.1；試行專案 integration manifest 亦標示 1.0.1，而 bundled workflow 的 1.0.0 是 workflow 版本。本機 `openspec --version` 回報 1.6.0，但未執行其 status／instructions，不能將上游 main 的欄位支援當成本機已具備。CLI、模板與專案自訂文件不是同一件事。判斷應以專案當下的文件與可用工具能力為準，不能用上游 main 取代本地規則。

### 12.2 共通語義與來源差異

| 所需資訊 | spec-kit | OpenSpec | Matt skills |
|---|---|---|---|
| 本次工作身分 | 明確 feature 及其目錄；依專案 resolver | 選定 change、schema、planning scope／root | 明確 spec／ticket 的檔案或平台身分 |
| 需求與限制 | spec、plan、constitution；相關 contracts 等 | 現行 specs 與此次 delta、proposal、design；依 schema 提供的 context | spec、ticket 驗收、相關 ADR 與術語 |
| 任務與依賴 | tasks 的 ID、階段、依賴文字與 [P] 提示 | schema 的 tracking artifact；task 順序及相關設計，不假定有逐項依賴圖 | 每張 ticket 的 Blocked by、驗收及狀態 |
| 完成狀態 | task 勾選與證據分開；checklist 可能代表需求品質 | artifact ready、task done、已驗證、已 sync、已 archive 分開 | ticket 狀態／驗收勾選與實作證據分開 |
| 後續生命週期 | converge 可能新增修正任務 | delta 同步及 change archive 可能改變來源位置 | ticket 拆分／關閉、spec 修改或後续決策 |

共通派工資訊只需包含來源引用、工作身分、需求／驗收、已知依賴、未知或衝突、當下責任範圍及驗證依據。這是供排程使用的摘要，不建立另一套可獨立修改的正式需求。

### 12.3 spec-kit 的具體適配要求

1. **先確認 feature，不猜目錄。** 試行專案的 `common.ps1` 優先使用 `SPECIFY_FEATURE_DIRECTORY`，其次讀 `.specify/feature.json`。不能只憑目前 branch 或最新 spec 編號選擇。
2. **解析亦可能寫入。** 同一份 helper 在沒有 `NoPersist` 時可能更新 feature.json；本地 prerequisite 的 `-PathsOnly` 分支才傳入 `-NoPersist`。未審查腳本副作用前，不為了讀取來源直接執行初始化、產生或解析命令。
3. **[P] 只是候選訊號。** 試行專案 008 tasks 的依賴段一面列平行機會，一面明定單一 writer；010、011 也保留此規則。要啟用多 writer，需先經授權調整現行政策，不由分類器暗中覆蓋。
4. **任務不等於派工粒度。** RED／GREEN 等數個 task 可共同構成一份 implementer brief；不必每個 checkbox 都啟動新 agent。
5. **保留流程 gate。** 本地 implement 把 requirements checklist 當唯讀品質關卡，不可因已批准任務計畫而自動勾選或略過。主動執行政策不是繞過來源工作流程的授權。
6. **Converge 會增加任務。** 本地技能及上游均會將缺口附加到 tasks。Handoff 不能因舊任務全勾選就判斷工作已結束。已批准需求內的修正與 brief 調整依現有自主權處理；改變交付、需求、驗收、範圍或重要設計時回到使用者確認。不得形成未選定的無限審查循環。

本地唯讀抽查範圍包含專案根目錄與 feature-008-spec worktree 的 008／011 任務，以及根目錄 010 任務；這些只是相容性樣本，沒有選定其中任何舊任務重新執行。011 另有付費 probe 需 owner 同意的限制，委派自主權不能取代該授權。

### 12.4 OpenSpec 的具體適配要求

1. **區分現行與目標行為。** Default spec-driven schema 的 delta 可新增、修改、移除或更名需求；只讀現行 specs 會漏掉變更，只讀 delta 則可能漏掉未改動的限制。
2. **不能固定 tasks.md 路徑。** 查閱版本的 apply 透過 status 及 instructions JSON 提供 schema、contextFiles、任務和 blocked／all_done 等狀態；自訂 schema 與 store 可改變文件所在位置。實際安裝版本不支援相關欄位時，需明確來源或回報能力缺口，不把查詢失敗解讀成零任務。
3. **文件依賴不等於 writer 依賴。** schema 的 proposal → specs／design → tasks 是產物依賴；不能直接轉成程式碼任務的平行排程。task 順序仍需結合設計與共用資源判斷。
4. **all_done、驗收通過與 archive 分開。** 上游 archive 可在使用者確認後帶未完成警告封存；因此 archive 不能當完成證據。Provost 不因交接或驗收而自行 archive／sync。
5. **恢復必須辨識來源搬移。** 同步可能更新主要 specs，封存會移動 change。保存 change 身分與來源位置，恢復時核對搬移及狀態，不因舊路徑失效重建已封存工作。
6. **保留 schema／流程限制。** apply 回報 blocked 時不開始實作；新增的一般自主管理政策不略過其必需產物或明確確認。

### 12.5 Matt skills 的具體適配要求

1. 維持既有 tracker 選擇；本地 spec／tickets、GitHub 或 GitLab 是來源位置差異，不要求轉寫成 spec-kit 的 tasks.md。
2. 以完整來源身分識別 ticket，不能只存裸 issue 編號。不同平台、repo 以及不同本地 feature 都可能有相同編號。
3. Blocked by 提供顯式任務依賴，但不保證檔案與執行資源獨立。缺少依賴資訊是未知，不能一律視為無依賴。
4. 原版刻意避免在長期 spec／tickets 寫死檔案路徑；當下寫入範圍仍在 brief 中由 main 補足。
5. 原版 handoff 只引用既有產物；本設計的歷史、過期辨識與恢復需另外實作。原版 commit 與 review 適配限制沿用第 3 節。

### 12.6 共用的控制邊界

來源流程負責需求與其自身 gate，main 負責依已批准計畫排程及整合；每個 worker 僅處理授予的範圍，不自行執行會遍歷全部 tasks 的外層 implement 流程。共用 tasks／tracker 的進度回寫由 main 協調，避免平行 writer 互相覆蓋。

文件缺失、未知依賴或互相衝突時先定位缺口。能由 repo 查明者自行查明；涉及需求決策或授權才問使用者。若來源已變更到讓既有派工依據失效，先重評受影響工作，不能繼續用舊快照發新任務。

### 12.7 補充驗收（待後續實作授權）

- spec-kit：兩個 feature、[P] 與單一 writer 同時存在、requirements checklist 非實作狀態、converge 新增任務、唯讀來源解析不寫 feature.json。
- OpenSpec：現行 spec 加 delta、自訂 schema／非預設來源、blocked apply、封存但仍有未完成警告、封存搬移後接續、CLI 缺失或版本能力不足。
- Matt：本地與遠端同編號不同 ticket、Blocked by 尚未完成、未提供依賴、已關閉但驗收證據不足。
- 共通：不改寫上游格式、不繞過來源 gate、不重複管理正式狀態；三種流程分別通過自己的案例後才標記對應支援程度。

首個真實專案仍使用既有 spec-kit 流程；它的成功不能替代 OpenSpec 或 Matt 的驗證。

### 12.8 可重查的上游來源

- [spec-kit implement](https://github.com/github/spec-kit/blob/730dcf53d04b4958e7013c7c448de11e9c8fa4a7/templates/commands/implement.md)
- [spec-kit tasks template](https://github.com/github/spec-kit/blob/730dcf53d04b4958e7013c7c448de11e9c8fa4a7/templates/tasks-template.md)
- [spec-kit converge](https://github.com/github/spec-kit/blob/730dcf53d04b4958e7013c7c448de11e9c8fa4a7/templates/commands/converge.md)
- [OpenSpec spec-driven schema](https://github.com/Fission-AI/OpenSpec/blob/bae58cf61479986431bb798acbe5a688a591c18c/schemas/spec-driven/schema.yaml)
- [OpenSpec apply](https://github.com/Fission-AI/OpenSpec/blob/bae58cf61479986431bb798acbe5a688a591c18c/skills/openspec-apply-change/SKILL.md)
- [OpenSpec archive](https://github.com/Fission-AI/OpenSpec/blob/bae58cf61479986431bb798acbe5a688a591c18c/skills/openspec-archive-change/SKILL.md)

查閱方式為 GitHub 官方倉庫網頁、API 固定 commit 的原始檔，以及本地 Get-Content／rg 唯讀抽查。未安裝或升級工具，未執行產生任務、實作、converge、sync、archive 或遠端 issue 寫入。
