# Gantt

以模板驅動的流程管理系統。使用者挑一份流程模板、填入參數與**目標完成日期**，系統依依賴圖**反推**出每個步驟該在何時開始與結束；步驟可由人手動回報完成，也可經由後端 API 自動完成。

核心體驗是「反推時程」加「進度追蹤」，不是傳統專案管理軟體的「手動拉時間軸」。

- [design.md](design.md) — UI/UX 與使用者流程
- [implement.md](implement.md) — 系統架構、資料模型、DSL 規格、排程演算法
- [AGENTS.md](AGENTS.md) — 開發約定與容易踩的坑

---

## 需要什麼

| 工具 | 版本 | 用途 |
|---|---|---|
| Python | 3.12+ | 後端 |
| [uv](https://docs.astral.sh/uv/) | — | Python 套件與環境管理 |
| Node.js | 20+ | 前端建置 |
| [pnpm](https://pnpm.io/) | 10+ | 前端套件管理 |
| PostgreSQL | 15+ | 正式環境資料庫（開發可用 SQLite） |

**不要用 `pip` / `venv` / `poetry` / `npm` / `yarn`** —— 見 [AGENTS.md](AGENTS.md#套件管理)。

---

## 五分鐘跑起來

以下用 SQLite，不需要安裝 PostgreSQL。

```bash
# 1. 安裝相依
uv sync

# 2. 設定環境（SESSION_SECRET 是必要的，憑證加密會拒絕使用預設值）
export DATABASE_URL="sqlite+aiosqlite:///$PWD/dev.db"
export SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

# 3. 建立資料表
uv run alembic upgrade head

# 4. 建立內建行事曆與第一個管理員（會提示輸入密碼）
uv run gantt seed

# 5. 匯入並發布一份範例模板
uv run gantt import examples/product_launch.yaml \
  -t examples/task_templates --publish

# 6. 啟動 API
uv run uvicorn app.api.main:app --reload
```

另開一個終端機啟動前端：

```bash
cd web
pnpm install
pnpm dev
```

打開 http://localhost:5173 ，用第 4 步建立的帳號登入。

要讓自動化步驟真的跑起來，再開一個終端機啟動 worker：

```bash
# 環境變數要跟 API 一致
export DATABASE_URL="sqlite+aiosqlite:///$PWD/dev.db"
export SESSION_SECRET="...同上..."
uv run python -m app.execution.worker
```

| 位址 | 內容 |
|---|---|
| http://localhost:5173 | 前端 |
| http://localhost:8000/api/v1/docs | Swagger（可直接試打 API） |
| http://localhost:8000/healthz | 存活檢查 |

---

## 不需要資料庫也能做的事

排程引擎是純函式，所以模板可以在完全不碰資料庫的情況下驗證。寫新模板時這是最快的迴路：

```bash
# 只檢查語法與參照
uv run gantt validate examples/product_launch.yaml -t examples/task_templates

# 填入參數，印出展開後的任務與依賴
uv run gantt expand examples/product_launch.yaml -t examples/task_templates \
  -p test_hours=16 -p line_type=A -r pm=alice -r qa_lead=bob

# 再給一個目標日期，反推出實際起訖時間與關鍵路徑
uv run gantt schedule examples/product_launch.yaml -t examples/task_templates \
  -T 2026-10-30T18:00 -p test_hours=16 -r pm=alice -r qa_lead=bob
```

`expand` 與 `schedule` 走的是跟建立 case 完全相同的程式碼路徑，所以這裡看到的日期就是實際會產生的日期。

**模板若含條件式任務（`when`），這一步是必要的而非便利功能**：那種模板的形狀取決於參數，靜態檢查無法確認展開後的圖是否合理。

---

## 測試

```bash
# 後端（468 則）
uv run pytest

# 只跑某一層
uv run pytest tests/scheduling -q
uv run pytest tests/execution -q

# 前端（26 則，Gantt 佈局數學）
cd web && pnpm test
cd web && pnpm typecheck
```

後端測試跑在 SQLite 上，不需要另外準備資料庫。

---

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://gantt:gantt@localhost:5432/gantt` | 開發可用 `sqlite+aiosqlite:///dev.db` |
| `SESSION_SECRET` | `change-me` | Session 簽章與憑證加密。**維持預設時憑證功能會拒絕運作** |
| `DEFAULT_TIMEZONE` | `Asia/Taipei` | CLI 與行事曆的預設時區 |
| `HTTP_HANDLER_ALLOWED_HOSTS` | `[]` | 內建 `http_request` handler 可連線的主機。**預設為空，代表拒絕一切外連** |
| `SHELL_HANDLER_ENABLED` | `false` | 內建 `shell_command` handler 是否啟用 |
| `SHELL_HANDLER_ALLOWED_COMMANDS` | `[]` | 啟用時的指令白名單 |
| `NOTIFICATION_CHANNELS` | `["email"]` | 啟用的外送管道 |
| `WORKER_POLL_INTERVAL_MS` | `1000` | worker 取件間隔 |

兩個 handler 的預設值是刻意的：由使用者可編輯的模板驅動的無限制外連就是 SSRF，能執行任意指令就是 RCE，所以預設必須是關閉而不是開放。要用就明確列出允許的主機或指令。

---

## 正式環境

四個部署單元。`api` 與 `worker` 共用同一份程式碼映像，只是進入點不同。

```
db       postgres:15（持久化 volume）
api      uvicorn app.api.main:app --workers 4
worker   python -m app.execution.worker（可水平擴充）
web      nginx 提供 web/dist 靜態檔，並反向代理 /api
```

```bash
uv run alembic upgrade head    # 部署前獨立執行
cd web && pnpm build           # 產出 web/dist
```

沒有 Redis 或訊息佇列 —— 工作佇列直接建在 PostgreSQL（`SELECT ... FOR UPDATE SKIP LOCKED`）。這讓「任務狀態」與「佇列狀態」天然在同一個交易裡一致，也少一個要維運的服務。理由見 [implement.md §1](implement.md)。

---

## 專案結構

```
app/
  dsl/           模板語言：schema(pydantic)、expressions、graph、expansion
  scheduling/    排程引擎（純函式，不碰 DB）：calendars、passes、analysis
  services/      業務邏輯：cases、templates、preview、snapshot、schedules
  execution/     執行引擎：registry、handlers、queue、runner、scans、worker
  notifications/ 通知建立與外送管道
  api/           FastAPI：routers、schemas、deps、統一錯誤格式
  models/        SQLAlchemy 資料表
  auth/          密碼雜湊與授權規則（純函式）
  cli.py         gantt expand / schedule / validate / import / seed
migrations/      Alembic
examples/        可直接跑的範例模板
tests/           對應 app/ 的結構
web/             前端（Vite + React + TS）
```

## 實作進度

階段 1–6 皆已完成，較薄的部分列在 [implement.md §13.1](implement.md)：模板編輯器沒有流程圖模式（表單模式唯讀、原始碼模式可編輯）、沒有 WebSocket 即時推送、「在兩個 task 之間插入 task」規格已定但尚未實作、Gantt 尚未接上列虛擬化。
