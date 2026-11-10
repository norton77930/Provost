# keep-awake

Windows 系統匣小工具，讓電腦與螢幕保持喚醒，用來取代 Orca 的「保持電腦處於喚醒狀態」。不需要系統管理員權限，也不用安裝任何東西（只用 Windows PowerShell 5.1 和 .NET Framework）。

## 模式

| 模式 | 效果 |
|---|---|
| 開啟（綠點） | `SetThreadExecutionState(ES_CONTINUOUS \| ES_SYSTEM_REQUIRED \| ES_DISPLAY_REQUIRED)`：不睡眠、螢幕不關 |
| 關（灰點） | 清除要求，回到正常的電源設定 |

- 左鍵點圖示：切換模式。右鍵：選單（開啟／關／結束）。
- 上次選的模式記在 `%LOCALAPPDATA%\keep-awake\mode.txt`，預設「開啟」。
- 只允許同時執行一份。程式結束或當掉時，Windows 會自動撤銷要求，電源設定不會被改動。

## 安裝／移除

```powershell
# 建立「啟動」資料夾捷徑（登入時自動執行），並立即啟動
powershell -NoProfile -ExecutionPolicy Bypass -File tools\keep-awake\keep-awake.ps1 -Install

# 移除捷徑（正在執行的那一份請從選單「結束」）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\keep-awake\keep-awake.ps1 -Uninstall
```

捷徑指向這個檔案目前的位置，請從固定的 checkout 執行 `-Install`（不要從之後會被刪掉的 worktree）。

## 確認有效

在系統管理員權限的終端機執行 `powercfg /requests`，`[DISPLAY]` 和 `[SYSTEM]` 底下應該會出現執行 `keep-awake.ps1` 的 `powershell.exe`。

## 注意

公司的群組原則如果強制螢幕保護程式鎖定，可能仍會鎖定畫面；這不影響背景工作，因為系統沒有進入睡眠。
