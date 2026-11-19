# 架構稽核重測（2026-11-19）

- 對照基準：`docs/analysis/2026-11-12-architecture-audit.md`（量測期間 2026-10-27 至 2026-11-12）
- 量測期間：最後一次安裝 `a0580f1`（2026-11-13T01:10Z）之後到 2026-11-19。
- S1 視窗（通知規則）：2026-11-13T01:45Z 起、到 2026-11-16T03:02:50Z 止所開始的主 session，紀錄也在該時間截斷（`~/.claude` 的 commit `08ddfa1` 又改了規則）。
- 性質：唯讀量測。工具見 `tools/audit/README.md`。以下數字為量測結果，不含 project 名稱或逐字稿內容。

## 結果

| 項目 | 基準 | 重測 | 目標 | 結果 |
|---|---|---|---|---|
| Stop gate 耗時 | 11-09..11-13 p90 22.8 s；4 天 57 次逾時 | 1,455 次執行，p50 3.95 s、p90 10.8 s；約 6 天 3 次逾時 | p90 < 10 s、0 次逾時 | 未達成 |
| SessionStart 逾時與耗時 | 14 次逾時 | 0 次逾時；匿名 hook p90 15.1 s、native-event p90 11.7 s | p90 < 10 s | 逾時已消除；耗時未達成 |
| `status` ÷ `record` | 0.55 | 124 ÷ 691 = 0.18 | < 0.2 | 達成 |
| 儀式性請求占比 | 同方法基準 15.1% | 安裝後 11.9%；S1 視窗 12.2% | < 9% | 未達成 |
| 只送通知的請求 ÷ 回合（S1 視窗，8 個 session） | 同方法基準 1,702 ÷ 2,900 = 0.587 | 116 ÷ 299 = 0.388（−34%） | 稽核的 0.53 → 0.25，換算此方法約 0.28 | 未達成 |
| 通知與其他工具合批 | 22% | 50% | — | 改善 |
| 推播後重述 | 0 | 0 | — | 無 |
| 驗收檔：全部 gate 通過但未結案 | 29 | 11 | 0（只算沒有待辦的檔案） | 見下 |
| main CI | — | `e29f208` 全綠 | — | 通過 |

細節：

- Stop gate：`hook-timing.js 2026-10-27T00:00:00Z 2026-11-09 2026-11-13` 的期間切法。
- 推播 116 次中：23 次在沒有其他工具的回合、65 次在該回合最後一個工具之後、28 次之後同回合還有其他工具。
- 通知比值要和同方法算出的基準比較。稽核報告的 0.53 是以 `measure.js` 的回合數為分母，該數字把 resume 的逐字稿重複計算（3,154，去重後 2,900），所以同方法的基準是 0.587，不是 0.53。
- 驗收檔的 11 個中有 5 個是 `D:/OSS/Provost` 的 mod 驗收檔，其 human gate 還沒通過（在等 owner，不是被遺忘），所以目標 0 應只計「沒有待辦」的檔案（`allDone`）。`count_acceptance.js` 先前把 6 個帶 UTF-8 BOM 的檔案算成無法解析（Stop gate 以 utf-8-sig 讀取，所以實際可解析）；本次變更已修正，並把「全部 gate 通過但未結案」拆成 `waitingHuman`（等 human gate）與 `allDone`（連 human gate 都通過）。

## 未量測

- `SessionStart:compact` 的子行程數與耗時：`time_sessionstart.py` 需要 store 副本，這次沒做。
- compaction 後的 context 遺失糾正次數。

## 重現指令

```
# S1 視窗：只送通知的請求 ÷ 回合、儀式性請求占比
NOSUB=1 SFROM=2026-11-13T01:45:00Z SUNTIL=2026-11-16T03:02:50Z UNTIL=2026-11-16T03:02:50Z node tools/audit/measure2.js 2026-11-13T01:45:00Z

# 同方法基準
NOSUB=1 UNTIL=2026-11-12T12:00:00Z node tools/audit/measure2.js 2026-10-27T00:00:00Z

# 驗收檔
node tools/audit/count_acceptance.js --list <含各 repo 的目錄> ...

# hook 耗時
node tools/audit/hook-timing.js 2026-10-27T00:00:00Z 2026-11-09 2026-11-13
```

預期輸出的摘要行：S1 視窗為 `push-only 116`、`TURNS 299`、`ratio 0.388`、`ceremony 12.2%`；基準為 `push-only 1702`、`TURNS 2900`、`ratio 0.587`、`ceremony 15.1%`。
