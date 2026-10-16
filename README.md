# ai-homework-line — LINE × Gemini 聊天機器人(含短期記憶)

一個可部署到雲端容器的 LINE 聊天機器人:用 LINE Messaging API 接收訊息,呼叫 Google Gemini 產生繁體中文回覆,並以「每位使用者最近 N 輪、閒置 M 分鐘過期」的短期記憶記住前文。

## 語言選擇

我也評估了 Rust(axum + reqwest):它的執行效率與記憶體用量較好,但這個服務的瓶頸是 Gemini 的網路延遲,不是 CPU;而且 LINE 和 Gemini 都沒有官方 Rust SDK,簽章驗證、型別、重試都得自己寫,作業風險與工時都較高。所以採用 **Python 3.12 + FastAPI**:LINE(`line-bot-sdk`)與 Gemini(`google-genai`)都有官方 SDK,測試生態也成熟(pytest、fakeredis)。

## 架構

```
LINE 平台 ──POST /callback──▶ FastAPI (app/main.py)
                                │ 1. 用原始 body 驗證 X-Line-Signature(失敗→400)
                                │ 2. 立即回 200,背景處理事件
                                ▼
                         app/bot.py ── 去重(webhookEventId)/ /reset / 非文字
                                │  同一對話加鎖,依序處理
                    ┌───────────┴───────────┐
           app/memory.py                 app/ai.py
   InMemoryStore | RedisStore      generate_reply(history, user_text)
                                   (google-genai,timeout + 429/5xx 重試)
```

| 檔案 | 用途 |
|---|---|
| `app/config.py` | 讀環境變數,缺必要變數直接拋錯退出 |
| `app/main.py` | FastAPI app factory、`/callback`、`/healthz`、LINE reply client |
| `app/bot.py` | 事件處理:去重、記憶、AI 呼叫、錯誤時道歉 |
| `app/memory.py` | `MemoryStore` 介面 + `InMemoryStore` / `RedisStore` |
| `app/ai.py` | Gemini 封裝(換供應商只改這個檔) |
| `scripts/simulate_webhook.py` | 端對端模擬(合法簽章 + 假訊息) |
| `scripts/smoke_gemini.py` | 真實 Gemini 冒煙測試(無 key 自動跳過) |
| `tests/` | pytest 單元測試(LINE、Gemini 全 mock) |
| `Dockerfile` / `render.yaml` | 容器與 Render 部署設定 |
| `VERIFICATION.md` | 驗證報告 |

## 重要設計說明

- **簽章驗證 fail-closed**:先拿原始 bytes 用 channel secret 做 HMAC-SHA256 比對;缺 header、簽章錯、body 被改、非 UTF-8 一律回 400,通過前不 parse JSON。
- **先回 200 再處理**:Gemini 可能要好幾秒,所以 webhook 驗簽後立刻回 200,事件交給背景處理,避免 LINE 等太久而重送。
- **去重**:用 `webhookEventId`(沒有時改用 message id)去重,保留 10 分鐘;Redis 版用 `SET NX EX`。
- **記憶格式**:內部存中性格式 `{"role": "user"|"assistant", "text": ...}`;送給 Gemini 前才把 `assistant` 轉成 `model`。
- **記憶過期**:記憶體版在讀取時檢查最後活動時間;Redis 版每次寫入用 `MULTI` pipeline 原子化執行 `RPUSH + LTRIM + EXPIRE`,靠 key TTL 過期。
- **併發**:同一對話的訊息在同一實例內以 `asyncio.Lock` 依序處理,第二則一定看得到第一則的回覆;跨實例則靠 Redis 原子寫入確保不遺失。
- **AI 失敗**:20 秒 timeout;只有 429 與 5xx 會重試,最多 2 次,指數退避 1s→2s;400 等其他錯誤與 timeout 不重試。失敗時回覆固定的道歉訊息並記 log。SDK 自帶的 HTTP 重試已關閉(`HttpRetryOptions(attempts=1)`);SDK 在 aiohttp 連線錯誤時內建的一次重連無法關閉,但受整體 timeout 限制。
- **安全過濾**:被擋(`prompt_feedback.block_reason`、`finish_reason=SAFETY`)或沒有文字時,回覆固定的友善訊息,不拋例外。
- **長度**:回覆超過 5000 字會截斷成 4999 字加「…」。
- **日誌**:不記使用者訊息全文與金鑰;只記事件類型、userId 的 SHA-256 前 10 碼、延遲與錯誤類型。

### ⚠️ 記憶後端注意事項

- `MEMORY_BACKEND=memory` 把記憶放在行程記憶體裡。**雲端容器重啟、重新部署或休眠喚醒後,記憶都會消失。**
- **實例數大於 1 時,記憶體版各實例的記憶不共享**(去重紀錄也不共享)。
- **正式部署請用 `MEMORY_BACKEND=redis`**;`render.yaml` 已預設使用 Render Key Value。

### Gemini 模型

`GEMINI_MODEL` 預設為 `gemini-3.8-flash`。依 Google AI 官方 release notes,它在 2026-09-02 GA,也是官方建議新專案使用的 Flash 模型。開發環境的 proxy 擋了 ai.google.dev,我沒能直接打開模型頁核對,**部署前請到 <https://ai.google.dev/gemini-api/docs/models> 確認模型代碼**。如需更換,改環境變數即可,不用改程式。

## 環境變數

| 變數 | 用途 | 必填 | 範例假值 |
|---|---|---|---|
| `LINE_CHANNEL_SECRET` | 驗證 webhook 簽章 | ✅ | `0123456789abcdef0123456789abcdef` |
| `LINE_CHANNEL_ACCESS_TOKEN` | 呼叫 reply API | ✅ | `xxxxxxxx...`(long-lived token) |
| `GEMINI_API_KEY` | Gemini API 金鑰 | ✅ | `AIzaSyFAKE...` |
| `GEMINI_MODEL` | 模型代碼 | 否(預設 `gemini-3.8-flash`) | `gemini-3.8-flash` |
| `MEMORY_BACKEND` | `memory` 或 `redis` | 否(預設 `memory`;Render 設 `redis`) | `redis` |
| `REDIS_URL` | Redis 連線字串 | `MEMORY_BACKEND=redis` 時必填 | `redis://red-xxxx:6379` |
| `MEMORY_MAX_TURNS` | 保留最近幾輪對話 | 否(預設 10) | `10` |
| `MEMORY_TTL_MINUTES` | 閒置幾分鐘後清除 | 否(預設 30) | `30` |
| `AI_TIMEOUT_SECONDS` | Gemini 單次呼叫 timeout | 否(預設 20) | `20` |
| `LOG_LEVEL` | 日誌等級 | 否(預設 `INFO`) | `INFO` |
| `PORT` | 監聽埠(Render/Cloud Run 自動注入) | 否(預設 8000) | `8000` |

## 本機開發

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 跑測試(全 mock,不需要任何金鑰)
pytest -q

# 端對端模擬(fake Gemini,不需要金鑰):證明記憶與簽章驗證
python scripts/simulate_webhook.py

# 真實 Gemini 冒煙測試(沒有 key 會顯示 SKIPPED)
GEMINI_API_KEY=你的key python scripts/smoke_gemini.py

# 啟動服務(先 cp .env.example .env 並填值)
set -a; source .env; set +a
uvicorn --factory app.main:create_app --port 8000 --reload
```

### 用 Docker 跑

```bash
docker build -t line-gemini-bot .
docker run --rm -p 8000:8000 --env-file .env line-gemini-bot
curl http://127.0.0.1:8000/healthz      # {"status":"ok"}

# 對容器送合法 / 非法簽章(--secret 要和容器的 LINE_CHANNEL_SECRET 相同)
python scripts/simulate_webhook.py --url http://127.0.0.1:8000 --secret "$LINE_CHANNEL_SECRET"
```

### 讓 LINE 打到本機(ngrok / cloudflared)

```bash
# 二擇一
ngrok http 8000
cloudflared tunnel --url http://localhost:8000
```

把印出的 `https://xxxx.ngrok-free.app` 或 `https://xxxx.trycloudflare.com` 加上 `/callback`,填進 LINE Developers Console 的 Webhook URL。每次重開通道網址都會變,要重填。

## 從零部署(Render + Docker)

### 1. 建立 LINE Messaging API channel
1. 到 <https://developers.line.biz/console/> 登入,建立 Provider。
2. 建立 **Messaging API** channel。新版流程可能需要先到 LINE Official Account Manager 建立官方帳號,再啟用 Messaging API。
3. 在 **Basic settings** 複製 **Channel secret** → 填入 `LINE_CHANNEL_SECRET`。
4. 在 **Messaging API** 分頁最下方 **Channel access token (long-lived)** 按 Issue → 填入 `LINE_CHANNEL_ACCESS_TOKEN`。
5. 用手機掃 **Messaging API** 分頁上的 QR code,把機器人加為好友。

### 2. 取得 Gemini API key
1. 到 Google AI Studio <https://aistudio.google.com/apikey> 按 **Create API key**。
2. **免費方案有速率限制(每分鐘請求數、每日請求數)**:超過時會回 429。程式會退避重試最多 2 次,還是失敗就回覆道歉訊息。測試時別連續狂傳,若看到道歉訊息請查 log 有沒有 `code=429`。

### 3. 在 Render 建立服務
1. 把這個 repo push 到 GitHub。
2. Render Dashboard → **New → Blueprint** → 選這個 repo,Render 會讀 `render.yaml`,建立:
   - `line-gemini-bot`:Docker Web Service(free),健康檢查 `/healthz`
   - `line-bot-memory`:Key Value(Redis 相容,free),`REDIS_URL` 自動帶入
3. 依提示填入三個 `sync: false` 的機密:`LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`、`GEMINI_API_KEY`。
4. 等部署完成,記下服務網址,例如 `https://line-gemini-bot-xxxx.onrender.com`。

> 不想用 Blueprint 的話:New → Web Service → 選 repo → Runtime 選 Docker → Health Check Path 填 `/healthz` → 在 Environment 填上表的變數。要用 Redis,就另外建一個 Key Value,把 Internal URL 填進 `REDIS_URL` 並設定 `MEMORY_BACKEND=redis`。

### 4. 設定 LINE Webhook
1. LINE Developers Console → 你的 channel → **Messaging API** 分頁。
2. **Webhook URL** 填 `https://<服務網址>/callback`。
3. 打開 **Use webhook**。
4. 在 **LINE Official Account features** 點 Auto-reply messages 的 Edit,到 Official Account Manager **關閉「自動回應訊息」**,也建議關閉「加入好友的歡迎訊息」;否則官方帳號會自己多回一句。
5. **先暖機**:Render 免費方案閒置約 15 分鐘就會休眠,喚醒要幾十秒。按 Verify 或展示前,先用瀏覽器打開一次 `https://<服務網址>/healthz`,等它回 `{"status":"ok"}`。
6. 按 **Verify**,應顯示 Success。

### 5. 部署後檢查清單
- [ ] `curl https://<服務網址>/healthz` 回 200 與 `{"status":"ok"}`
- [ ] LINE Console 的 Verify 顯示 Success
- [ ] 手機連續傳三則訊息(例如「我叫小明」→「我喜歡貓」→「我叫什麼、喜歡什麼?」),第三則回答要用到前兩則的內容
- [ ] 傳 `/reset`,收到「已忘掉」的回覆;再問「我叫什麼?」,它應該不知道
- [ ] 傳一張貼圖或圖片,收到「目前只支援文字」
- [ ] Render Logs 裡沒有訊息全文、沒有金鑰

## (選用)改用 Google Cloud Run

```bash
PROJECT=your-project REGION=asia-east1
gcloud services enable run.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com

# 把金鑰放進 Secret Manager
printf '%s' "$LINE_CHANNEL_SECRET"       | gcloud secrets create line-channel-secret --data-file=-
printf '%s' "$LINE_CHANNEL_ACCESS_TOKEN" | gcloud secrets create line-channel-access-token --data-file=-
printf '%s' "$GEMINI_API_KEY"            | gcloud secrets create gemini-api-key --data-file=-
# 給 Cloud Run 的 service account 讀取權限(預設 compute SA)
for s in line-channel-secret line-channel-access-token gemini-api-key; do
  gcloud secrets add-iam-policy-binding $s \
    --member="serviceAccount:$(gcloud projects describe $PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done

gcloud run deploy line-gemini-bot --source . --region $REGION --allow-unauthenticated \
  --min-instances=1 --max-instances=3 \
  --set-env-vars=GEMINI_MODEL=gemini-3.8-flash,MEMORY_BACKEND=redis,REDIS_URL=redis://<你的 Redis>:6379 \
  --set-secrets=LINE_CHANNEL_SECRET=line-channel-secret:latest,LINE_CHANNEL_ACCESS_TOKEN=line-channel-access-token:latest,GEMINI_API_KEY=gemini-api-key:latest
```

`--min-instances=1` 用來避免冷啟動(會產生持續費用)。Cloud Run 預設可能開多個實例,**務必使用 redis 後端**(例如 Upstash 免費方案的 `rediss://` URL)。Cloud Run 會注入 `PORT`,Dockerfile 已經處理。

驗證細節請見 [VERIFICATION.md](VERIFICATION.md)。
