# 驗證報告

驗證環境:Linux 雲端沙盒、Python 3.12.3、Docker 29.8.2,2026-10-16。沙盒的對外網路只能經過受限 proxy(LINE API、Gemini API、Docker Hub 都無法直接連線),所以所有外部服務都用 mock 或 fake。

## ✅ 已實際驗證

| 項目 | 指令 | 結果摘要 |
|---|---|---|
| 單元測試 | `pytest -q` | **42 passed**(LINE 與 Gemini 全 mock,Redis 用 fakeredis) |
| 測試確實有效(故意改壞) | 逐一改壞程式後跑 pytest | 拿掉 per-user 鎖 → 2 failed;拿掉 assistant→model 轉換 → 2 failed;拿掉去重 → 1 failed;拿掉 429 重試 → 2 failed;拿掉 Redis TTL → 2 failed |
| 端對端模擬 | `python scripts/simulate_webhook.py` | 真的 HTTP 服務(uvicorn)+ 合法簽章 + fake Gemini。第 2、3 則回覆帶出前文「我叫小明,我喜歡貓」;`/reset` 之後回覆「(沒有前文)」;錯誤簽章與缺少簽章都回 400 |
| Docker build | `docker build -t line-gemini-bot .` | 成功,image 338MB(python:3.12-slim)。見下方附註 |
| 容器以非 root 執行 | `docker exec lb id` | `uid=10001(appuser)` |
| 缺環境變數 fail-fast | `docker run --rm -e PORT=8080 line-gemini-bot` | exit code 1,`ConfigError: 缺少必要環境變數: LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, GEMINI_API_KEY` |
| 監聽 `$PORT` | `-e PORT=8080` | `Uvicorn running on http://0.0.0.0:8080` |
| `/healthz` | `curl http://127.0.0.1:8080/healthz` | `{"status":"ok"} -> HTTP 200`,不含任何設定值 |
| `/callback` 合法簽章 | curl + `openssl dgst -hmac` 產生簽章 | `{"status":"ok"} -> HTTP 200` |
| `/callback` 錯誤簽章 / 缺 header / body 被竄改 | curl | 三者都是 `invalid signature -> HTTP 400` |
| Verify 按鈕情境(`events: []`) | curl | HTTP 200 |
| 重送同一事件 | 同一個 body 送兩次 | 第二次 log 出現 `duplicate event skipped`,沒有再呼叫 AI |
| AI 失敗不崩潰(容器內) | 容器連不到 Gemini | log 出現 `ai failed ... reason=unexpected`,接著嘗試回覆道歉訊息,服務持續運作 |
| 日誌不洩漏 | 檢查 `docker logs` | 只有事件數、userId 雜湊(例如 `user=316ca0efda`)、延遲與錯誤類型;沒有訊息全文、沒有金鑰 |
| 真實 Redis 後端 | `redis:7-alpine` 容器 + `RedisStore` | 5 次併發寫入後依視窗保留 4 筆(2 輪),TTL=1800,去重 `True/False`,reset 後清空 |

附註:Docker Hub 回 429(rate limit),沙盒 proxy 的 CA 也不在 image 內,所以我在本地(不進 repo)把 `mirror.gcr.io/library/python:3.12-slim` 加上沙盒 CA,tag 成 `python:3.12-slim` 當 base。**repo 內的 Dockerfile 一字未改**,在一般環境(Render)直接從 Docker Hub 拉 base image 即可。

## ❌ 未能驗證

| 項目 | 原因 |
|---|---|
| 真實 Gemini 呼叫(`scripts/smoke_gemini.py`) | 沙盒沒有 `GEMINI_API_KEY`,腳本輸出 `SKIPPED`。**未驗證**:預設模型 `gemini-3.8-flash` 是否能用、system_instruction 的實際效果 |
| 預設模型名稱 | ai.google.dev 被 proxy 擋住,只能從搜尋結果裡的官方 release notes 摘要得知 `gemini-3.8-flash` 已 GA,沒有直接打開官方模型頁核對 |
| 真實 LINE 端對端(手機傳訊、reply API 實際送達) | 沒有 LINE channel 與金鑰,沙盒也連不到 api.line.me |
| 真實 Render 部署、`render.yaml` 語法、Render Key Value 連線 | 沒有 Render 帳號;`render.yaml` 依官方 Blueprint 格式撰寫,未經 Render 實際解析 |
| Cloud Run 指令 | 只提供指令,沒有執行 |
| 多實例共享記憶 | 只用單一 Redis 加併發寫入驗證原子性,沒有實際開兩個服務實例 |

## 🙋 需要你手動做的步驟

1. 建立 LINE Messaging API channel,取得 Channel secret 與 long-lived access token。
2. 在 Google AI Studio 建立 Gemini API key,然後在本機執行 `GEMINI_API_KEY=... python scripts/smoke_gemini.py`,確認輸出 `OK` 而且回答有提到「小明」。若出現 404 / 模型不存在,到官方模型頁換一個可用的 `GEMINI_MODEL`。
3. 把 repo 推到 GitHub,用 Render Blueprint 部署,填入三個機密環境變數。
4. 在 LINE Console 設定 Webhook URL `https://<服務網址>/callback`,開啟 Use webhook,關閉自動回應訊息。
5. 先打一次 `/healthz` 暖機,再按 Verify。
6. 照 README 的「部署後檢查清單」用手機實測:連續三則訊息確認記憶,`/reset` 確認清空。
