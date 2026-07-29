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

## 技術棧

後端 FastAPI + SQLAlchemy 2.0 (async) + PostgreSQL 15+ / Alembic / pytest。
前端 React 18 + TypeScript + Vite / TanStack Query / Zustand / Vitest / Playwright。

選型理由見 [implement.md §2](implement.md)。要換掉其中任何一項前先確認該節的論證是否還成立。

## 目錄配置

```
app/            後端；api 與 worker 共用同一份程式碼，只是進入點不同
  scheduling/   排程引擎（純函式，不碰 DB）
  execution/    task 執行引擎與 handler registry
  handlers/     task_api 的處理函式，啟動時自動掃描註冊
web/            前端（package.json 與 pnpm-lock.yaml 放這裡）
  src/
migrations/     Alembic
```

## 常用指令

專案尚未 scaffold，以下為建立後的預期指令：

```bash
uv sync                                      # 安裝後端相依
uv run uvicorn app.main:app --reload         # 啟動 API
uv run python -m app.worker                  # 啟動 worker
uv run pytest                                # 後端測試
uv run alembic upgrade head                  # 套用遷移
uv run alembic revision --autogenerate -m "" # 產生遷移

cd web
pnpm install
pnpm dev                                     # 前端開發伺服器
pnpm test                                    # Vitest
pnpm build
```

## 注意事項

- **排程引擎必須維持純函式**（輸入任務、依賴、行事曆、目標日期 → 輸出時間），不得存取資料庫。同一份邏輯要同時支撐建立 case、試算預覽、與影響模擬三種用途。
- **Case 快照不可變。** `template_snapshot` 沒有任何更新路徑；模板改版不得影響進行中的 case。
- **Baseline 建立後不再改變**，唯一例外是明確的 reset-baseline 操作。事後插入的任務 baseline 為 NULL。
- 新增 DSL 欄位時，同時更新 implement.md 的 §4 規格、§4.7 驗證規則、與 §4.15 展開管線的順序。
