# TW Daytrade Desk

每日 **07:30（Asia/Taipei）** 台股當沖觀察報告追蹤站。

- 規則：Hermes screener v1（量價／當沖／注意處置／週轉／5 日均量）+ 夜盤閘門  
- 更新方式：**僅排程或手動跑報告腳本時**寫入 `data/`，並以 **SSH** push 到本 repo  
- 本站為靜態頁，**不構成投資建議**

## 公開網址（GitHub Pages）

**https://wt-jerry.github.io/tw-daytrade-desk/**

Repo：https://github.com/WT-Jerry/tw-daytrade-desk  
Remote：`git@github.com:WT-Jerry/tw-daytrade-desk.git`

## 本機預覽

```bash
cd ~/.hermes/www/daytrade-tracker
python3 -m http.server 8765
# http://127.0.0.1:8765/
```

## 手動推送

```bash
python3 ~/.hermes/scripts/finance/push_daytrade_tracker_github.py
```

07:30 cron 會跑 `daytrade_report_0730.py`，預設在更新 tracker 後自動 push（可用 `--no-github` 關閉）。

## 資料結構

```
index.html
assets/
data/index.json
data/reports/YYYYMMDD.json
```
