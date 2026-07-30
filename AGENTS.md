# AGENTS.md

## 專案

以模板驅動的 Gantt 流程管理系統。使用者挑選流程模板、填入參數與目標日期，系統依依賴圖**反推**出每個步驟的起訖時間；步驟可手動或經由後端 API 自動標記完成。

- [design.md](design.md) — UI/UX 與使用者流程
- [implement.md](implement.md) — 系統架構、資料模型、DSL 規格、排程演算法

動到排程語義、DSL 欄位或狀態機時，**先讀 implement.md 對應章節**，並同步更新文件——兩份文件互相引用，欄位名與狀態名必須一致。

## 套件管理

**後端 Python 一律用 `uv`，前端一律用 `pnpm`。**

| 用途 | 指令 |
|---|---|
| 安裝相依 | `uv sync` ／ `cd web && pnpm install` |
| 新增套件 | `uv add <pkg>` ／ `pnpm add <pkg>` |
| 新增開發相依 | `uv add --dev <pkg>` ／ `pnpm add -D <pkg>` |
| 移除套件 | `uv remove <pkg>` ／ `pnpm remove <pkg>` |
| 執行指令 | `uv run <cmd>` ／ `pnpm <script>` |

規則：

- **不要用 `pip`、`python -m venv`、`poetry`**。也不需要手動 activate venv——`uv run` 會自行處理。
- **不要用 `npm` 或 `yarn`**。若出現 `package-lock.json` 或 `yarn.lock`，那是誤用的產物，刪掉。
- `uv.lock` 與 `web/pnpm-lock.yaml` **要進版控**。
- 直接改 `pyproject.toml` / `package.json` 的相依區塊後，記得跑 `uv lock` / `pnpm install` 讓 lockfile 同步。

## 程式碼慣例

- **所有註解與 docstring 一律用英文**，設計文件維持中文。
- 行寬 79（`ruff` 設定，與編輯器的 linter 一致）。提交前跑 `uv run ruff format` 與 `uv run ruff check`。
- 註解寫「為什麼」而非「做什麼」；規格相關的決定要標出 implement.md 的章節（如 `§4.15`）。

## 技術棧

後端 FastAPI + SQLAlchemy 2.0 (async) + PostgreSQL 15+ / Alembic / pytest。
前端 React 18 + TypeScript + Vite / TanStack Query / Zustand / Vitest / Playwright。

選型理由見 [implement.md §2](implement.md)。要換掉其中任何一項前先確認該節的論證是否還成立。

## 目錄配置

```
app/
  dsl/          模板語言：schema(pydantic)、loader、expressions、graph、expansion
  models/       SQLAlchemy 資料表
  auth/         密碼雜湊與授權規則（純函式）
  services/     業務邏輯：cases / preview / snapshot / calendars / identity
  api/          FastAPI：routers、schemas、deps、統一錯誤格式
  cli.py        gantt expand / schedule / validate / seed
  config.py     設定；db.py 連線與 session
  scheduling/   排程引擎（純函式，不碰 DB）：calendars / passes / analysis
  execution/    task 執行引擎與 handler registry ← 階段 5
migrations/     Alembic
examples/       可直接跑的範例模板
tests/          對應 app/ 的結構
web/            前端（package.json 與 pnpm-lock.yaml 放這裡）← 階段 4
```

## 常用指令

```bash
uv sync                                      # 安裝相依
uv run pytest                                # 測試
uv run ruff format app/ tests/ && uv run ruff check app/ tests/

# 展開模板看結果（不需要資料庫）
uv run gantt expand examples/product_launch.yaml \
  -t examples/task_templates -p test_hours=16 -r pm=alice -r qa_lead=bob
uv run gantt validate examples/product_launch.yaml -t examples/task_templates

# 由目標日期反推出實際日期（也不需要資料庫）
uv run gantt schedule examples/product_launch.yaml \
  -t examples/task_templates -T 2026-08-28T18:00 -p test_hours=16 \
  -r pm=alice -r qa_lead=bob

uv run alembic upgrade head                  # 套用遷移
uv run alembic revision --autogenerate -m "" # 產生遷移
uv run gantt seed                            # 建立內建行事曆與第一個管理員

uv run uvicorn app.api.main:app --reload     # 啟動 API（/api/v1/docs 有 Swagger）
```

`DATABASE_URL` 預設指向 PostgreSQL。沒有 Postgres 時可用
`DATABASE_URL="sqlite+aiosqlite:///dev.db"` 跑遷移——模型的 JSON 欄位與部分索引
都有 SQLite variant。

## 注意事項

- **排程引擎必須維持純函式**（輸入任務、依賴、行事曆、目標日期 → 輸出時間），不得存取資料庫。同一份邏輯要同時支撐建立 case、試算預覽、與影響模擬三種用途。
- **進出行事曆的 datetime 一律帶時區**，naive 值要拒絕而非假設為 UTC。
- 改動行事曆算術時，`tests/scheduling/test_calendars.py` 有一組對照「逐分鐘暴力法」的 property test——手算的期望值很容易錯，那組才是真正的防線。
- **Case 快照不可變。** `template_snapshot` 沒有任何更新路徑；模板改版不得影響進行中的 case。
- **Baseline 建立後不再改變**，唯一例外是明確的 reset-baseline 操作。事後插入的任務 baseline 為 NULL。
- 新增 DSL 欄位時，同時更新 implement.md 的 §4 規格、§4.7 驗證規則、與 §4.15 展開管線的順序。
- **所有時間欄位一律用 `TZDateTime`**（`UtcDateTime` TypeDecorator）。它會拒絕寫入 naive 值、並保證讀回來一定帶時區——PostgreSQL 的 TIMESTAMPTZ 本來就會，SQLite 不會，排程引擎則完全不接受 naive。
- **enum 欄位一律用 `enum_type(...)`** 而非 `String`。用 `String` 時值會以純字串讀回，任何 `is SomeEnum.MEMBER` 比較都會無聲失敗（不是報錯，是靜靜走錯分支）。
- 每個會改動日期的操作都要走「鎖定 case → 套用變更 → 全量重算 → 寫稽核」這個形狀，見 `app/services/cases.py`。
