# Gantt 流程管理系統 — 實作文件（系統架構）

本文件描述系統架構、資料模型、演算法與 API 規格。
使用者體驗與畫面設計請見 [design.md](design.md)。

---

## 1. 架構總覽

```
┌──────────────────────────────────────────────────────────────────┐
│                          瀏覽器                                   │
│   React + TypeScript + Vite                                      │
│   ├─ Gantt 渲染層 (自建 SVG)                                     │
│   ├─ Template 編輯器 (表單 / 流程圖 / Monaco YAML)                │
│   └─ TanStack Query (伺服器狀態) + Zustand (UI 狀態)             │
└───────────────┬──────────────────────────────┬───────────────────┘
                │ REST (JSON)                  │ WebSocket
                ▼                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                       API 服務 (FastAPI)                          │
│   ├─ 路由層          認證、權限檢查、請求驗證 (Pydantic)          │
│   ├─ 服務層          case / task / template 的業務邏輯            │
│   ├─ 排程引擎        backward pass、forecast pass、critical path  │
│   ├─ Template DSL    解析、驗證、參數展開、快照                    │
│   └─ 事件匯流排      狀態變更 → 通知 + WebSocket 推送             │
└───────────────┬──────────────────────────────┬───────────────────┘
                │                              │
                ▼                              ▼
┌───────────────────────────┐   ┌─────────────────────────────────┐
│      PostgreSQL           │   │   Worker 程序（獨立部署）        │
│  ├─ 業務資料表            │◀─▶│   ├─ 觸發迴圈：ready → 執行     │
│  ├─ task_runs (執行紀錄)  │   │   ├─ 輪詢迴圈：running → 查狀態 │
│  └─ job_queue (SKIP LOCKED)│   │   ├─ 逾時 / 重試處理            │
└───────────────────────────┘   │   └─ Handler Registry           │
                                │        @task_handler(...)        │
                                └─────────────────────────────────┘
```

**四個部署單元**：Web 靜態檔、API 服務（可水平擴充）、Worker 程序（可水平擴充）、PostgreSQL。

不引入 Redis / RabbitMQ。工作佇列直接建在 PostgreSQL，用 `SELECT ... FOR UPDATE SKIP LOCKED` 實作多 worker 安全取件。以本系統的規模（同時進行的 case 數量級為百至千），這個選擇省下一整個中介軟體的維運成本，且讓「任務狀態」與「佇列狀態」天然在同一個交易裡保持一致。

---

## 2. 技術選型

| 層 | 選擇 | 理由 |
|---|---|---|
| 後端框架 | FastAPI | Pydantic 型別驗證與 Template DSL 的 schema 驗證天然契合；自動產生 OpenAPI 給前端生成 client |
| ORM | SQLAlchemy 2.0 (async) | 成熟的關聯查詢能力，遞迴 CTE 支援 DAG 查詢 |
| 資料庫 | PostgreSQL 15+ | 需要 JSONB（快照、參數）、遞迴 CTE（依賴圖）、`SKIP LOCKED`（佇列）、交易隔離 |
| 遷移 | Alembic | — |
| 前端框架 | React 18 + TypeScript + Vite | — |
| 伺服器狀態 | TanStack Query | 快取失效與樂觀更新的處理最完整 |
| UI 狀態 | Zustand | 縮放層級、篩選、drawer 開合等輕量狀態 |
| Gantt 渲染 | **自建 SVG**（見 §9.2） | 現成套件不支援 baseline/forecast 雙軌 |
| YAML 編輯 | Monaco + `yaml` + JSON Schema | 自動補完與即時錯誤標示 |
| 流程圖編輯 | React Flow | DAG 節點拖曳與連線 |
| 即時推送 | WebSocket (FastAPI 原生) | — |
| 測試 | pytest / pytest-asyncio、Vitest、Playwright | — |

---

## 3. 資料模型

### 3.1 使用者與權限

```sql
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT,                    -- 若接 SSO 則為 NULL
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    is_template_admin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE groups (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,      -- 對應 DSL 的 task_group
    display_name TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE group_members (
    group_id  BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id   BIGINT NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    is_lead   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (group_id, user_id)
);
```

### 3.2 行事曆

```sql
CREATE TABLE calendars (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,      -- 'continuous' 為內建保留名稱
    timezone    TEXT NOT NULL DEFAULT 'Asia/Taipei',
    -- working_hours: {"mon":[["09:00","18:00"]], ..., "sat":[], "sun":[]}
    working_hours JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- holidays: ["2026-01-01", "2026-02-16"]
    holidays    JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_builtin  BOOLEAN NOT NULL DEFAULT FALSE
);
```

系統預載兩筆：`continuous`（24×7，`is_builtin = TRUE`）與 `taiwan_office`（週一至週五 09:00–18:00）。

### 3.3 Task 模板

```sql
CREATE TABLE task_templates (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,    -- DSL 的 task_name，如 'bt1'
    display_name  TEXT NOT NULL,
    duration_default TEXT NOT NULL,        -- '10H'
    schedule_mode TEXT NOT NULL DEFAULT 'continuous',
                                           -- 'continuous' | 'business'
    calendar_id   BIGINT REFERENCES calendars(id),
    -- para_schema: [{"para_name":"my_para1","para_type":"str",
    --                "para_default":null,"required":false}]
    para_schema   JSONB NOT NULL DEFAULT '[]'::jsonb,
    task_api      TEXT,                    -- handler 名稱，NULL = 僅手動
    api_mode      TEXT,                    -- 'trigger_poll'|'trigger_callback'|'poll_only'
    api_timeout_s        INTEGER DEFAULT 1800,
    api_retry_max        INTEGER DEFAULT 3,
    api_retry_interval_s INTEGER DEFAULT 300,
    api_poll_interval_s  INTEGER DEFAULT 60,
    allow_manual_override BOOLEAN NOT NULL DEFAULT TRUE,
    created_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 3.4 Gantt 模板

```sql
CREATE TABLE gantt_templates (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL,            -- DSL 的 template_name
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL,            -- 'draft' | 'published' | 'archived'
    definition   JSONB NOT NULL,           -- 完整 DSL（見 §4）
    change_note  TEXT,
    created_by   BIGINT REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    UNIQUE (name, version)
);

-- 每個模板名稱最多只能有一份草稿
CREATE UNIQUE INDEX uq_gantt_template_draft
    ON gantt_templates(name) WHERE status = 'draft';
```

已發布的版本**不可修改**（由服務層強制）。編輯已發布模板的行為是「基於它建立新草稿」。

### 3.5 Gantt Case

```sql
CREATE TABLE gantt_cases (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    template_name TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    -- 建立當下的完整 DSL 快照，含所有引用到的 task_template 定義。
    -- 日後模板改版完全不影響本 case。
    template_snapshot JSONB NOT NULL,
    -- 使用者填入的參數值：{"my_para1": 3, "my_para2": "test"}
    params        JSONB NOT NULL,
    -- 角色 → user_id 綁定：{"pm": 17, "qa_lead": 23}（見 §4.10）
    role_assignments JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- 因 when 為 false 而未生成的任務，供 UI 列出（見 §4.11）
    -- [{"id":"safety_review","label":"安規審查","when":"para.line_type == 'A'"}]
    skipped_tasks JSONB NOT NULL DEFAULT '[]'::jsonb,

    target_date   TIMESTAMPTZ NOT NULL,
    -- 專案緩衝（§5.8）：backward pass 的起點為 target_date - buffer_seconds
    buffer_seconds INTEGER NOT NULL DEFAULT 0,
    -- target_date 變更歷程：[{"from":"...","to":"...","by":17,"at":"...","note":"客戶延期"}]
    target_date_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- baseline 重設紀錄；每次重設將前一份 baseline 完整存入
    baseline_resets JSONB NOT NULL DEFAULT '[]'::jsonb,

    status        TEXT NOT NULL DEFAULT 'active',
                                        -- 'active'|'completed'|'cancelled'
    -- 排程引擎算出的快取欄位，每次重算時更新
    forecast_end  TIMESTAMPTZ,
    health        TEXT,                 -- 'on_track'|'at_risk'|'overdue'
    buffer_consumed_ratio NUMERIC(5,4), -- 緩衝消耗比例，見 §5.8
    progress_ratio        NUMERIC(5,4), -- 工期加權完成比例，見 §5.9

    owner_id      BIGINT REFERENCES users(id),
    -- 由客戶端產生，防止連點建出重複 case（§8.2）
    idempotency_key TEXT UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    archived_at   TIMESTAMPTZ,          -- 封存後不出現在預設清單，見 §10.2
    version       INTEGER NOT NULL DEFAULT 1   -- 樂觀鎖
);

CREATE INDEX idx_cases_status_target ON gantt_cases(status, target_date);
CREATE INDEX idx_cases_health ON gantt_cases(health) WHERE status = 'active';
```

### 3.6 Case Task

```sql
CREATE TABLE case_tasks (
    id            BIGSERIAL PRIMARY KEY,
    case_id       BIGINT NOT NULL REFERENCES gantt_cases(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,          -- case 內唯一
    display_name  TEXT NOT NULL,
    source_task_template TEXT,            -- 快照來源，僅供追溯
    phase         TEXT,                   -- 視覺分組標籤（§4.13）

    -- 排程輸入
    duration_seconds INTEGER NOT NULL,    -- 已展開參數並解析後的秒數
    schedule_mode TEXT NOT NULL DEFAULT 'continuous',
    calendar_id   BIGINT REFERENCES calendars(id),

    -- Baseline：建立 case 時反推的計畫，之後不再改變。
    -- 【可為 NULL】事後插入的任務不在原始計畫中，沒有 baseline 可言（§5.10）。
    -- NULL 即代表「計畫外新增」，Gantt 對這類任務只畫單軌預測 bar。
    baseline_start TIMESTAMPTZ,
    baseline_end   TIMESTAMPTZ,

    -- Forecast：依實際進度重算，每次狀態變更時更新
    forecast_start TIMESTAMPTZ NOT NULL,
    forecast_end   TIMESTAMPTZ NOT NULL,

    -- Actual：實際發生
    actual_start   TIMESTAMPTZ,
    actual_end     TIMESTAMPTZ,

    status        TEXT NOT NULL DEFAULT 'pending',
                  -- 'pending'|'ready'|'running'|'done'|'failed'|'cancelled'
    completion_source TEXT,               -- 'manual' | 'api'
    completed_by  BIGINT REFERENCES users(id),
    completion_note TEXT,

    owner_id      BIGINT REFERENCES users(id),
    -- owner 的來源，供「重新指派角色」判斷可否批次覆寫（§4.10）
    -- 'role:pm' | 'group_lead' | 'same_as:my_task1' | 'literal' | 'manual'
    -- 'manual' 表示使用者在 case 內個別改過，批次重新指派不會動它
    owner_source  TEXT NOT NULL DEFAULT 'literal',
    group_id      BIGINT REFERENCES groups(id),
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- API 設定（自快照展開，允許 case 層級覆寫）
    task_api      TEXT,
    api_mode      TEXT,
    api_config    JSONB NOT NULL DEFAULT '{}'::jsonb,
    allow_manual_override BOOLEAN NOT NULL DEFAULT TRUE,

    -- 失敗策略與可選性（§4.12）
    on_failure    TEXT NOT NULL DEFAULT 'block',   -- 'block'|'continue'|'cancel_case'
    is_optional   BOOLEAN NOT NULL DEFAULT FALSE,
    warn_before_seconds INTEGER NOT NULL DEFAULT 7200,

    is_on_critical_path BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    version       INTEGER NOT NULL DEFAULT 1,   -- 樂觀鎖
    UNIQUE (case_id, name)
);

CREATE INDEX idx_case_tasks_case ON case_tasks(case_id);
CREATE INDEX idx_case_tasks_owner_status ON case_tasks(owner_id, status);
CREATE INDEX idx_case_tasks_ready ON case_tasks(status)
    WHERE status IN ('ready', 'running');
```

### 3.7 依賴（DAG 邊）

```sql
CREATE TABLE task_dependencies (
    id            BIGSERIAL PRIMARY KEY,
    case_id       BIGINT NOT NULL REFERENCES gantt_cases(id) ON DELETE CASCADE,
    predecessor_id BIGINT NOT NULL REFERENCES case_tasks(id) ON DELETE CASCADE,
    successor_id   BIGINT NOT NULL REFERENCES case_tasks(id) ON DELETE CASCADE,
    -- 前置完成後的等待時間（§4.3）；bypass 略過節點時會累加
    lag_seconds    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (predecessor_id, successor_id),
    CHECK (predecessor_id <> successor_id),
    CHECK (lag_seconds >= 0)
);

CREATE INDEX idx_deps_pred ON task_dependencies(predecessor_id);
CREATE INDEX idx_deps_succ ON task_dependencies(successor_id);
```

依賴以獨立資料表儲存（而非 `case_tasks` 上的陣列欄位），因為需要雙向高效查詢：backward pass 需要「某 task 的所有後繼」，forecast pass 需要「某 task 的所有前置」。

### 3.8 API 執行紀錄與佇列

```sql
CREATE TABLE task_runs (
    id           BIGSERIAL PRIMARY KEY,
    case_task_id BIGINT NOT NULL REFERENCES case_tasks(id) ON DELETE CASCADE,
    attempt      INTEGER NOT NULL,
    handler_name TEXT NOT NULL,
    status       TEXT NOT NULL,   -- 'running'|'succeeded'|'failed'|'timeout'
    request_payload  JSONB,
    response_payload JSONB,
    external_ref TEXT,            -- 外部系統的 job id，供輪詢使用
    error_message TEXT,
    error_detail  TEXT,           -- 完整堆疊
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    UNIQUE (case_task_id, attempt)
);

CREATE TABLE job_queue (
    id           BIGSERIAL PRIMARY KEY,
    job_type     TEXT NOT NULL,   -- 'trigger'|'poll'|'timeout_check'|'recalc'
    case_task_id BIGINT REFERENCES case_tasks(id) ON DELETE CASCADE,
    case_id      BIGINT REFERENCES gantt_cases(id) ON DELETE CASCADE,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    run_after    TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts     INTEGER NOT NULL DEFAULT 0,
    locked_by    TEXT,
    locked_at    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_job_queue_pickup ON job_queue(run_after)
    WHERE locked_by IS NULL;
```

### 3.9 稽核與通知

```sql
CREATE TABLE audit_events (
    id          BIGSERIAL PRIMARY KEY,
    case_id     BIGINT REFERENCES gantt_cases(id) ON DELETE CASCADE,
    case_task_id BIGINT REFERENCES case_tasks(id) ON DELETE CASCADE,
    actor_id    BIGINT REFERENCES users(id),   -- NULL = 系統
    event_type  TEXT NOT NULL,
        -- 'case.created' | 'case.cancelled' | 'task.updated'
        -- 'task.completed' | 'task.inserted' | 'task.deleted'
        -- 'task.api_triggered' | 'task.api_failed' | 'schedule.recalculated'
    before_state JSONB,
    after_state  JSONB,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_case ON audit_events(case_id, created_at DESC);

CREATE TABLE notifications (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    case_id     BIGINT REFERENCES gantt_cases(id) ON DELETE CASCADE,
    case_task_id BIGINT REFERENCES case_tasks(id) ON DELETE CASCADE,
    read_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 去重鍵，見下方說明；由服務層組出，NULL 表示此類通知不去重
    dedup_key   TEXT
);

CREATE INDEX idx_notif_unread ON notifications(user_id, created_at DESC)
    WHERE read_at IS NULL;

CREATE UNIQUE INDEX uq_notif_dedup ON notifications(dedup_key)
    WHERE dedup_key IS NOT NULL;

-- 外送管道的投遞紀錄；站內通知不需要此表
CREATE TABLE notification_deliveries (
    id              BIGSERIAL PRIMARY KEY,
    notification_id BIGINT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,      -- 'email'|'slack'|'teams'|'line'|'webhook'
    status          TEXT NOT NULL,      -- 'pending'|'sent'|'failed'
    error_message   TEXT,
    sent_at         TIMESTAMPTZ,
    UNIQUE (notification_id, channel)
);

-- 每位使用者對每種通知類型選擇的管道
CREATE TABLE notification_preferences (
    user_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type      TEXT NOT NULL,
    channels  TEXT[] NOT NULL DEFAULT '{}',   -- 站內一律開啟，此處僅列外送管道
    PRIMARY KEY (user_id, type)
);
```

**去重鍵的組成**：`{user_id}:{scope}:{scope_id}:{type}:{epoch}`，例如 `17:task:128:task.late_start:0`。三個要點：

- **必須含 `user_id`**。同一事件常要通知多人（逾期未完成 → task owner + case owner），少了它第二個收件人就會被去重掉，變成只有一個人收到通知。
- **scope 明確區分 `task` 與 `case`**，不靠 `case_task_id` 是否為 NULL 來判斷。案例層級的通知若以 NULL 參與唯一性比較，PostgreSQL 的 `NULLS NOT DISTINCT` 會讓不同 case 的同類通知互相碰撞。
- **`epoch`** 在任務被 `reopen`（還原完成）或重試時遞增，讓同一個任務可以重新進入告警週期。否則一個任務逾期通知過一次之後，就永遠不會再提醒了。

不需要去重的通知（例如「你被指派為負責人」，同一人可能被指派多次）`dedup_key` 留 NULL。

**管道可插拔**：`NotificationChannel` 是與 §6.1 的 handler registry 同形的介面（`async def send(notification, target) -> DeliveryResult`），實作放在 `app/notifications/channels/`。首版提供 `email`；Slack / Teams / LINE 之後只需新增一個檔案並在設定中啟用，不動核心邏輯。投遞失敗不影響站內通知，僅記錄於 `notification_deliveries` 並重試三次。

---

## 4. Template DSL 規格

### 4.1 Gantt 模板

原始構想的正式化版本。**所有新增欄位皆為選填且有預設值，且舊欄位名保留為別名**（見 §4.9），因此原始範例可原封不動解析成功。

```yaml
gantt:
  template_name: my_template_name
  dsl_version: 1                      # DSL schema 版本，供日後遷移
  version: 3                          # 模板版本，發布時由系統遞增
  description: 新產品導入流程          # 選填
  buffer: 8H                          # 專案緩衝，見 §5.8；預設 0

  schedule:                           # 選填，週期性自動建立 case，見 §4.15
    cron: "0 9 5 * *"                 # 每月 5 號 09:00
    timezone: Asia/Taipei
    target_date_offset: 3D            # 目標日期 = 建立時間 + 3 天
    name_template: "{{ now.year }}年{{ now.month }}月關帳"
    enabled: true

  roles:                              # 見 §4.10；建立 case 時才綁定實際使用者
    - name: pm
      display_name: 專案經理
      required: true
    - name: qa_lead
      display_name: 品保負責人
      default_group: 品保部

  template_para:
    - para_name: my_para1
      para_type: int                  # int|float|str|bool|date|enum
      para_default: 1
      required: true                  # 預設 true
      group: 產能設定                  # 選填，建立精靈的欄位分組標題
      description: 測試階段的允許工時
      validation:                     # 選填
        min: 1
        max: 10
    - para_name: my_para2
      para_type: str
      para_default: test
    - para_name: line_type            # enum 範例
      para_type: enum
      choices: [A, B, C]
      para_default: A

  flow:
    - phase: 準備階段                  # 見 §4.13，純視覺分組，不影響依賴
      tasks:
        - id: my_task1                # 原 task_name；case 內唯一，requirement 參照它
          uses: tt1                   # 原 task_template
          label: 需求確認              # 原 display_name，選填
          owner: { role: pm }         # 見 §4.10
          group: my_group_name
          requirement: none           # 見 §4.3
          duration: 12H               # 原 target_duration

    - phase: 測試階段
      tasks:
        - id: functional_test
          uses: tt2
          label: 功能測試
          owner: { role: qa_lead }
          group: my_group_name2
          duration: 12H
          requirement:
            - task: my_task1
              lag: 4H                                # 見 §4.3，前置完成後再等 4H
          schedule_mode: business                    # 覆寫 task 模板
          calendar: taiwan_office
          task_para:                                 # 覆寫 task 模板參數預設值
            my_para1: "{{ para.my_para2 }}"

        - id: safety_review
          uses: tt6
          when: "{{ para.line_type == 'A' }}"        # 見 §4.11，false 則不生成
          owner: { group_lead: 品保部 }
          requirement: my_task1
          duration: 6H

    - phase: 結案階段
      tasks:
        - id: my_task3
          uses: tt3
          owner: { same_as: my_task1 }               # 與需求確認同一人
          group: my_group_name3
          duration: 12H
          requirement: [functional_test, safety_review]   # 多前置，全部完成

        - id: notify
          uses: tt7
          requirement: my_task3
          duration: 10M
          on_failure: continue                       # 見 §4.12
          optional: true
```

**最小寫法仍然有效。** 不使用 `roles` / `phase` / `when` 時，`flow` 可直接是扁平的任務陣列，`owner` 可直接寫使用者名稱——即原始構想的形式。

### 4.2 Task 模板

```yaml
task:
  id: bt1                              # 原 task_name
  label: 資料備份                       # 原 display_name，選填
  default_duration: 10H                # 原 task_duration_default
  schedule_mode: continuous            # continuous | business
  calendar: continuous
  default_owner: { group_lead: 資訊部 } # 選填，flow 未指定 owner 時的後備
  warn_before: 2H                      # 選填，逾期預警提前量，預設 2H
  task_para:
    - para_name: my_para1
      para_type: str                   # 選填，預設 str
      para_default: null
    - para_name: my_para2
      para_type: int
      para_default: 1
  task_api: my_function                # 省略則為「僅手動完成」
  api_mode: trigger_poll               # 見 §4.6
  api_timeout: 30M
  api_retry_max: 3
  api_retry_interval: 5M
  api_poll_interval: 60S
  allow_manual_override: true
  on_failure: block                    # 見 §4.12，可被 flow 節點覆寫
```

### 4.3 `requirement` 的寫法

| 寫法 | 語義 |
|---|---|
| 省略 / `none` / `null` / `[]` | 無前置，為流程起點 |
| `my_task1` | 單一前置 |
| `[my_task1, my_task2]` | 多前置，**全部完成**才可開始（AND 語義） |
| `{ task: my_task1, lag: 4H }` | 單一前置 + 延遲 |
| `[my_task1, { task: my_task2, lag: 30M }]` | 簡寫與完整寫法可混用 |

解析後一律正規化為 `[{task, lag_seconds}]`。

**Lag 語義**：後續任務的最早開始時間 = 前置結束時間 + lag。Lag 期間不佔用任何人力（養護、冷卻、等待對方回覆等），Gantt 上以兩條 bar 之間的虛線間隔呈現。Lag 以**後續任務**的行事曆換算，因此 `business` 模式下的 lag 會跳過非工作時間。不接受負值——負 lag 實質上是 SS 依賴，見 §14。

目前不支援 OR 語義；若日後需要，將以獨立的 `requirement_mode: all \| any` 欄位擴充，不改變現有寫法。

### 4.4 參數引用語法

任何字串值都可包含 `{{ ... }}` 運算式，於**建立 case 時一次性求值**並寫入快照。求值後的結果存進 `case_tasks`，執行期不再重新求值。

可用命名空間：

| 命名空間 | 內容 | 範例 |
|---|---|---|
| `para.*` | 使用者填入的模板參數 | `{{ para.my_para1 }}` |
| `case.*` | `case.name`、`case.target_date`、`case.created_at` | `{{ case.name }} - 檢驗` |
| `role.*` | 建立 case 時綁定的角色使用者名稱（§4.10） | `{{ role.pm }}` |

支援的運算：四則運算、比較運算（`==` `!=` `<` `>` `<=` `>=`）、布林運算（`and` / `or` / `not`）、`in`、括號、字串串接，以及白名單函式 `int()` / `float()` / `str()` / `round()` / `max()` / `min()` / `len()`。

**次方運算未開放**，因為規格只需要四則運算，而 `2 ** 9999999` 是廉價的 DoS 向量。字串與陣列的重複（`'x' * n`）另有長度上限保護。

實作使用受限的 AST 求值器（`ast.parse` + 白名單節點走訪），**不使用 `eval`**。屬性存取、下標、函式定義、匯入、推導式一律拒絕。求值失敗時建立 case 的請求整筆失敗並回報具體位置。

**可用於**：`duration`、`owner`、`group`、`label`、`task_para` 的值、`when`、`lag`。

**不可用於** `id` 與 `requirement`——流程結構必須在模板層就是靜態可驗證的，否則驗證器無法在發布前偵測循環依賴。

### 4.5 Duration 格式

正規表示式：`^\d+(\.\d+)?\s*[SMHD]$`（大小寫不敏感）

| 後綴 | 單位 | 換算 |
|---|---|---|
| `S` | 秒 | 1 |
| `M` | 分 | 60 |
| `H` | 小時 | 3600 |
| `D` | 天 | 86400（`continuous`）／ **1 個工作日**（`business`） |

`business` 模式下的 `D` 依該行事曆的每日工時計算（例如 09:00–18:00 為 9 小時）。這個差異在 UI 上明確標示，避免誤解。

不支援複合寫法（`1D12H`）。需要時寫成 `36H`。

#### `schedule_mode` 與 `calendar` 的關係

兩個欄位表達的是同一件事，解析規則只有一條（`app/dsl/schema.py` 的 `resolve_calendar`）：

1. 有寫 `calendar:` → 用它，`schedule_mode` 不再影響選擇。
2. 沒寫 `calendar:` 且 `schedule_mode: business` → 用預設辦公行事曆 `taiwan_office`。
3. 其餘 → `continuous`。

也就是說 **`schedule_mode` 是「哪一份行事曆」的簡寫，不是獨立開關**。這條規則同時被建立 case 的展開流程與之後每次重算使用；先前兩邊各自判斷，結果不一致：展開端把 `calendar` 的預設值寫死為 `continuous`，所以只宣告 `schedule_mode: business` 而沒有指名行事曆的任務被排成 24×7——宣告了工時卻完全沒有生效，而且沒有任何訊息。重算端則相反，會把明明指名了辦公行事曆、但 `schedule_mode` 仍是 `continuous` 的任務強制拉回 24×7，使同一個 case 的 baseline 與 forecast 用不同基準計算。

既有 case 不受影響：行事曆名稱在建立時就寫進快照，這正是快照隔離要保證的事（§4.8）。

### 4.6 `api_mode` 三種模式

| 模式 | 行為 |
|---|---|
| `trigger_poll` | Task 轉 `ready` 時呼叫 handler 觸發；handler 回傳 `external_ref`；worker 依 `api_poll_interval` 呼叫 handler 的 `poll` 方法查詢狀態直到完成或逾時。**適合長時間非同步作業。** |
| `trigger_callback` | 觸發後不輪詢，等待外部系統呼叫 `POST /api/v1/callbacks/{token}` 回報結果。系統產生一次性 token 隨觸發參數傳出。 |
| `poll_only` | 不主動觸發，僅依間隔呼叫 handler 的 `poll` 查詢「這件事完成了嗎」。適合系統只有讀取權限的情境。 |

`trigger_poll` 為預設。

### 4.7 驗證規則

模板發布前必須通過。分為阻擋發布的**錯誤**與僅提示的**警告**（對應 [design.md §9.2](design.md#92-即時驗證)）：

**錯誤**

| 代碼 | 說明 |
|---|---|
| `E_DUP_TASK_NAME` | `task_name` 在同一模板內重複 |
| `E_UNKNOWN_REQUIREMENT` | `requirement` 參照不存在的 `task_name` |
| `E_CYCLE` | 依賴圖存在環（回報完整環路） |
| `E_UNKNOWN_TASK_TEMPLATE` | `uses` 參照不存在的 Task 模板 |
| `E_BAD_DURATION` | `duration` 不符 §4.5 格式 |
| `E_ALIAS_CONFLICT` | 同一節點同時使用正式欄位名與舊別名（§4.9） |
| `E_MIXED_FLOW_FORM` | `flow` 混用扁平陣列與 `phase` 分段兩種寫法（§4.13） |
| `E_UNKNOWN_PARAM` | `{{ para.x }}` 中的 `x` 未在 `template_para` 定義 |
| `E_BAD_EXPRESSION` | 運算式語法錯誤或使用了白名單外的節點 |
| `E_UNKNOWN_CALENDAR` | `calendar` 參照不存在的行事曆 |
| `E_MISSING_FIELD` | 必填欄位缺漏 |
| `E_UNKNOWN_ROLE` | `owner.role` 未在 `roles` 宣告 |
| `E_UNKNOWN_SAME_AS` | `owner.same_as` 參照不存在的任務 id |
| `E_SAME_AS_CYCLE` | `owner.same_as` 形成循環 |
| `E_NEGATIVE_LAG` | `lag` 為負值 |
| `E_BAD_WHEN` | `when` 求值結果非布林值 |
| `E_ALL_TASKS_SKIPPED` | 所有任務都被 `when` 濾掉，產生空流程 |

**警告**

| 代碼 | 說明 |
|---|---|
| `W_UNUSED_PARAM` | 參數定義後未被任何任務引用 |
| `W_MULTIPLE_ROOTS` | 多個無前置的起點 |
| `W_MULTIPLE_SINKS` | 多個無後繼的終點（各自對齊目標日期） |
| `W_ZERO_DURATION` | 工期為 0 |
| `W_UNREGISTERED_API` | 引用的 `task_api` 在後端 registry 中不存在 |
| `W_UNASSIGNED_OWNER` | 任務未指定 `owner` 且無任何後備來源 |
| `W_MULTI_GROUP_LEAD` | `group_lead` 對應到多位 lead，將取第一位 |
| `W_CONDITIONAL_SINK` | 受 `when` 控制的節點同時是終點，條件為 false 時會改變 case 的終點結構 |
| `W_CONSTANT_WHEN` | `when` 為常數（永遠 true 或永遠 false），可能是筆誤 |
| `W_UNUSED_ROLE` | `roles` 宣告後未被任何任務使用 |

**驗證的時機限制**：`when` 的結果取決於建立 case 時填入的參數，因此模板層驗證只能檢查**語法與參照**，無法確認過濾後的圖形（例如「條件都為 false 時流程會不會斷開」）。這使得 [design.md §9.3](design.md#93-試算預覽) 的**試算預覽**從便利功能升級為必要的驗證手段——編輯器應在模板含有 `when` 時主動提示管理員至少試算一組參數。

### 4.8 Case 快照格式

建立 case 時，`gantt_cases.template_snapshot` 存入**自我包含**的 JSON：

```json
{
  "snapshot_version": 1,
  "captured_at": "2026-07-29T10:00:00Z",
  "gantt": { "...完整 gantt 模板 DSL..." },
  "task_templates": {
    "tt1": { "...完整 task 模板 DSL..." },
    "tt2": { "..." },
    "tt3": { "..." }
  },
  "calendars": {
    "continuous":    { "working_hours": {}, "holidays": [] },
    "taiwan_office": { "working_hours": {...}, "holidays": [...] }
  }
}
```

**行事曆也一併快照**，否則日後修改假日表會回頭改變既有 case 的計算基準。

快照建立後不可變。使用者在 case 內編輯 task 或插入 task 時，改的是 `case_tasks` 資料表，快照僅作為「原始定義」的追溯依據。

### 4.9 欄位正名與相容別名

原始構想中 `task_name` 一詞有兩種語義：在 task 模板裡是「模板識別碼」，在 `flow` 裡是「實例識別碼」，而引用模板卻叫 `task_template`。這在寫模板時很容易搞混，因此正名如下。

| 位置 | 正式欄位名 | 舊名（永久保留為別名） |
|---|---|---|
| task 模板 | `id` | `task_name` |
| task 模板 | `label` | `display_name` |
| task 模板 | `default_duration` | `task_duration_default` |
| flow 節點 | `id` | `task_name` |
| flow 節點 | `uses` | `task_template` |
| flow 節點 | `label` | `display_name` |
| flow 節點 | `duration` | `target_duration` |
| flow 節點 | `owner` | `task_owner` |
| flow 節點 | `group` | `task_group` |

解析器同時接受兩組名稱，同一節點內**兩者並存時視為錯誤**（`E_MISSING_FIELD` 的變體 `E_ALIAS_CONFLICT`）。序列化回 YAML 時一律輸出正式名稱；Template 編輯器的 YAML 模式在偵測到舊名時提供「一鍵正名」動作。

`task_para` 在兩處的形態不同是**刻意的**：task 模板裡是參數**定義**清單（含型別與預設值），flow 節點裡是參數**值**的對應表。文件與編輯器都應明確標示這個區別。

### 4.10 `owner` 解析規則

把使用者名稱寫死在模板裡（`task_owner: my_user_name`）意味著這個模板每次跑都是同一個人，模板實質上不可重用。改為宣告角色，建立 case 時才綁定。

```yaml
roles:
  - name: pm
    display_name: 專案經理
    required: true                # 建立 case 時必須指派
  - name: qa_lead
    display_name: 品保負責人
    default_group: 品保部          # 人員選擇器預設篩選此群組
```

`owner` 支援五種寫法，依序嘗試解析：

| 寫法 | 解析方式 |
|---|---|
| `owner: my_user_name` | 直接視為 username（向後相容） |
| `owner: "{{ para.requester }}"` | 求值後視為 username |
| `owner: { role: pm }` | 從 case 的 `role_assignments` 取得 |
| `owner: { group_lead: 品保部 }` | 該群組 `is_lead = TRUE` 的成員；多位時取第一位並發 `W_MULTI_GROUP_LEAD` |
| `owner: { same_as: my_task1 }` | 複製指定任務的 owner |

未指定 `owner` 時的後備順序：flow 節點 → phase 的 `default_owner` → task 模板的 `default_owner` → template 的 `default_owner` → NULL（未指派，UI 顯示「待指派」且該任務轉 `ready` 時通知 group lead）。

`same_as` 在展開與過濾完成後、依拓撲順序解析，因此可以鏈式引用；形成循環時回報 `E_SAME_AS_CYCLE`。若被引用的任務因 `when` 為 false 而不存在，則往上追溯其來源。

**為什麼不直接用參數。** 在 `template_para` 加一個 `pm: str` 也能達成類似效果，但系統無從得知那是「人」而非字串，於是無法提供人員選擇器、無法做權限檢查、無法寄送通知。角色是一級概念。

建立 case 的精靈因此多一個「角色指派」區塊（[design.md §3 步驟 2](design.md#3-流程-a從-template-建立-case)），指派結果存於 `gantt_cases.role_assignments`。

### 4.11 條件式任務 `when`

```yaml
- id: safety_review
  uses: tt6
  when: "{{ para.line_type == 'A' }}"
  requirement: my_task1
```

沒有這個欄位，「要不要跑安規審查」這種分支只能做成兩份模板，參數組合一多就會爆炸成 2ⁿ 份。

**語義**

- 求值為 false 的節點**完全不生成** `case_tasks` 列，不是生成後標為 cancelled
- 被略過節點的依賴**自動接回**：對每一組 (前置 p, 後繼 s) 補上邊 `p → s`，lag 相加
- 連續多個節點被略過時遞移處理（依拓撲順序逐一 bypass）
- 被略過節點若無前置，其後繼單純少掉這條 requirement

**關鍵限制：`when` 只在建立 case 時求值一次。** 若允許執行期依實際結果改變流程結構，baseline 就無法在建立時算出來，整個「由目標日期反推」的模型會失去意義。需要執行期分支的場景（例如「檢驗不通過才做返工」）目前的做法是：把返工任務設為 `optional` 並預設 `cancelled`，由使用者手動啟用。

Case 詳情頁會列出因條件而略過的任務清單（唯讀），讓使用者知道模板裡還有哪些步驟這次沒跑。

### 4.12 失敗策略 `on_failure` 與 `optional`

目前 API 失敗一律卡住等人介入，但有些步驟失敗不該擋住主線。

```yaml
- id: notify_stakeholders
  uses: tt7
  on_failure: continue      # block(預設) | continue | cancel_case
  optional: true
```

| `on_failure` | 行為 |
|---|---|
| `block`（預設） | 任務停在 `failed`，下游維持 `pending`，等待重試或手動完成 |
| `continue` | 任務仍標為 `failed`（保留紀錄與告警），但**視同已結算**放行下游；case health 標記為含失敗 |
| `cancel_case` | 整個 case 轉為 `cancelled`，所有未完成任務一併取消並通知 |

這使 §6.2 的放行條件從「前置全部 `done`」改為「前置全部**已結算**」：

```python
def is_settled(task) -> bool:
    if task.status in ('done', 'cancelled'):
        return True
    return task.status == 'failed' and task.on_failure == 'continue'
```

`optional: true` 的語義刻意收窄為三點，避免與 `on_failure` 混淆：

1. 不列入關鍵路徑標記（§5.5）
2. Case 判定完成時不納入必要條件——其餘任務全部 `done` 時，未完成的 optional 任務自動轉 `cancelled` 並通知其 owner
3. 進度分母仍包含它（使用者需要知道還有東西沒做）

**optional 不代表延遲不會傳播。** 若有必要任務依賴這個 optional 任務，它的延遲照樣推遲下游——這是圖的結構決定的，不是旗標能改變的。

### 4.13 `phase` 視覺分段

12 個步驟以上的 `flow` 攤平成單一陣列就難以閱讀。

```yaml
flow:
  - phase: 準備階段
    default_owner: { role: pm }      # 選填，此段的 owner 後備
    tasks: [ ... ]
  - phase: 測試階段
    tasks: [ ... ]
```

**純視覺分組，不影響依賴語義**——跨 phase 的 `requirement` 完全合法，phase 也不隱含順序。存入 `case_tasks.phase`，Gantt 可依此摺疊、Template 編輯器可分頁呈現。

扁平寫法（`flow` 直接是任務陣列）永遠支援；兩種寫法不可在同一模板內混用（`E_MISSING_FIELD` 的變體 `E_MIXED_FLOW_FORM`）。

### 4.14 建立期展開管線

`when` / 運算式 / 依賴解析的**執行順序影響結果**，因此明訂如下。整條管線是純函式，同一組輸入必然產生同一個圖，這是快照與試算預覽能一致的前提。

**一個 flow 節點最多產生一個 task。** 這個不變量讓依賴處理保持簡單：task 的 id 就是節點 id，因此 bypass 之後留下的邊直接就是最終結果，不需要任何投影。

| # | 階段 | 說明 |
|---|---|---|
| 1 | 展平 phase | 保留 `phase` 標籤，得到扁平節點清單 |
| 2 | 套用別名 | 舊欄位名正規化為正式名稱（§4.9） |
| 3 | 驗證參數值 | 對照 `template_para` 檢查型別、必填、`validation` |
| 4 | 求值 `when` | 節點層級，false 者標記為略過 |
| 6 | 求值其餘運算式 | `duration`、`label`、`owner`、`task_para`、`lag` |
| 7 | 解析依賴 | 展開組引用、略過節點的 bypass 接回、lag 相加 |
| 8 | 解析 owner | 依拓撲順序處理 `same_as` 與各種後備 |
| 9 | 圖驗證 | 循環偵測、孤立節點、空流程 |
| 10 | Backward pass | 寫入 baseline（§5.2） |
| 11 | Forward pass | 寫入 forecast、關鍵路徑、health（§5.3） |

步驟 1–9 產出的中間結構同時供**建立 case**（落地）與 **`/preview` 試算**（不落地）使用，確保兩者結果一致。

### 4.15 週期性自動建立

「月結關帳」這類流程本來就是固定週期跑的，不該每個月要人手動建一次。模板可宣告排程：

```yaml
schedule:
  cron: "0 9 5 * *"                       # 每月 5 號 09:00（標準 5 欄 cron）
  timezone: Asia/Taipei
  target_date_offset: 3D                  # 目標日期 = 建立時間 + 3 天
  name_template: "{{ now.year }}年{{ now.month }}月關帳"
  params:                                 # 選填，覆寫參數預設值
    my_para2: monthly
  role_assignments:                       # 選填，未指定則沿用上次建立時的指派
    pm: finance_manager
  enabled: true
```

存於獨立資料表（`gantt_templates.definition` 之外），因為排程狀態需要獨立於模板版本更新：

```sql
CREATE TABLE template_schedules (
    id            BIGSERIAL PRIMARY KEY,
    template_name TEXT NOT NULL UNIQUE,
    cron          TEXT NOT NULL,
    timezone      TEXT NOT NULL DEFAULT 'Asia/Taipei',
    target_date_offset_seconds INTEGER NOT NULL,
    name_template TEXT,
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,
    role_assignments JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at   TIMESTAMPTZ,
    last_case_id  BIGINT REFERENCES gantt_cases(id) ON DELETE SET NULL,
    next_run_at   TIMESTAMPTZ NOT NULL,
    created_by    BIGINT REFERENCES users(id)
);
```

**行為約定**

- 排程一律使用該模板**目前的最新已發布版本**，不釘選版本——週期性流程通常希望跟著最新定義走
- 建立時以 `template_schedules.id + next_run_at` 產生 `idempotency_key`，確保多 worker 或補跑不會建出重複 case
- 建立失敗（例如必填角色未指派、參數驗證不過）不重試，改為通知 `created_by` 並停用排程，避免每分鐘失敗一次洗版
- 錯過的執行不補建（例如系統停機三天）。補建過期的流程幾乎沒有意義，只會製造一堆一建立就逾期的 case；改為在下次啟動時通知「已錯過 N 次排程」

由 worker 的排程巡檢（§6.3）順帶處理：每分鐘掃描 `enabled = TRUE AND next_run_at <= now()`。

---

## 5. 排程引擎

引擎是純函式模組（`app/scheduling/`），輸入為任務集合、依賴邊、行事曆與目標日期，輸出為時間。**不觸碰資料庫**，因此可完整單元測試，也可直接用於「預覽」而不落地。

### 5.1 Calendar 介面

```python
class Calendar(Protocol):
    @property
    def day_seconds(self) -> int:
        """一個工作日的秒數，用於換算 `D` 單位的工期。"""

    def add(self, start: datetime, seconds: int) -> datetime:
        """自 start 起算，前進 `seconds` 個工作秒數。"""

    def sub(self, end: datetime, seconds: int) -> datetime:
        """自 end 起算，倒退 `seconds` 個工作秒數。"""

    def next_working_instant(self, t: datetime) -> datetime:
        """若 t 落在非工作時間，回傳下一個工作時刻；否則回傳 t。"""

    def previous_working_instant(self, t: datetime) -> datetime:
        """若 t 落在非工作時間，回傳上一個工作時刻；否則回傳 t。"""

    def elapsed(self, start: datetime, end: datetime) -> int:
        """兩個時刻之間的工作秒數，下限為 0。"""
```

`elapsed` 是 forward pass 判斷「進行中的 task 做了多少」所需：若用時鐘時間相減，一個掛在週末的 `business` task 會白得到 48 小時的進度。

**`day_seconds` 的取法**：`business` 行事曆取「非零工時中出現次數最多的那個長度」（同票取較長者），而非平均值——這樣半天班的週五不會把「一天」拉低。可在建立行事曆時明確指定覆寫。`continuous` 恆為 86400。

**所有進出行事曆的 datetime 必須帶時區**，naive 值直接拒絕而不預設為 UTC：排程引擎裡一個無聲的時區偏移事後幾乎不可能查出來。

兩個實作：

- **`ContinuousCalendar`** — `add` / `sub` 即單純的加減，`next_working_instant` 恆等。
- **`BusinessCalendar`** — 依 `working_hours` 與 `holidays` 逐區段累加/扣減。為避免逐分鐘迴圈，實作以「日」為單位跳躍：先計算需要跨越幾個完整工作日，再處理首尾不完整的區段。跨年度的假日查詢以 set 快取。

每個 task 各自持有 Calendar 實例（依 §4.5 的 `resolve_calendar`），因此**同一個 case 內不同 task 可用不同計算基準**。

**所有時間運算一律在 UTC 上進行，只有「哪一天、哪個工作區間」才換算回當地時間。** 這不是風格偏好。Python 明文規定：兩個 aware datetime 若共用同一個 `tzinfo`，相減會**忽略時區位移**，直接以牆上時鐘計算。而所有工作區間端點都由同一個 `ZoneInfo` 產生，因此區間長度一旦用當地時間相減，在時區轉換當天就是錯的——夏令時間往前撥的那天，09:00–18:00 只有 8 小時實際時間，卻會被算成 9 小時。`add` / `sub` / `elapsed` / 兩個 snapping 函式全部受影響，方向是「憑空多出或少掉一小時的工時」。

```python
>>> a = datetime(2026, 3, 8, 0, 0, tzinfo=ZoneInfo("America/New_York"))
>>> b = datetime(2026, 3, 8, 6, 0, tzinfo=ZoneInfo("America/New_York"))
>>> (b - a).total_seconds() / 3600          # 6.0，錯
>>> (b.astimezone(UTC) - a.astimezone(UTC)).total_seconds() / 3600   # 5.0
```

`day_seconds`（`1D` 的換算基準）刻意維持名目長度：它是「一個工作天算幾小時」的常數，不隨個別日期伸縮。

### 5.2 Backward Pass（計算 baseline）

建立 case 時執行一次，結果寫入 `baseline_start` / `baseline_end` 後**永不改變**。

```python
def backward_pass(tasks, edges, target_date) -> dict[TaskId, tuple[datetime, datetime]]:
    order = topological_sort(tasks, edges)     # 若有環則拋 CycleError
    result = {}

    # 專案緩衝：所有計畫都往前挪，緩衝留在末端（§5.8）
    plan_deadline = target_date - buffer_seconds

    for task in reversed(order):               # 由終點往起點
        successors = edges.successors_of(task.id)
        if not successors:
            end = plan_deadline                # 終點對齊「目標日期 − 緩衝」
        else:
            # 後繼的開始時間扣掉該條邊的 lag，取最早者
            end = min(
                lag_back(succ, result[succ.id][0], edge.lag_seconds)
                for succ, edge in successors
            )

        cal = calendar_for(task)
        end = cal.previous_working_instant(end)
        start = cal.sub(end, task.duration_seconds)
        result[task.id] = (start, end)

    return result
```

核心規則兩條：

1. **無後繼的 task** → `end = target_date − buffer`
2. **有後繼的 task** → `end = min(所有後繼的 start − 該條邊的 lag)`，然後 `start = calendar.sub(end, duration)`

`lag_back(succ, succ_start, lag)` 以**後續任務**的行事曆倒推 lag（`calendar_for(succ).sub(succ_start, lag)`）。lag 屬於「等待」而非「工作」，但仍受行事曆約束——`business` 模式下 lag 會跳過非工作時間，因為「等 4 小時後接手」的前提是有人在上班。

多個終點各自對齊 `target_date`，這是刻意的：模板若有兩條並行的收尾分支，兩條都該在目標日期完成。驗證器以 `W_MULTIPLE_SINKS` 提示這個行為。

### 5.3 Forward Pass（計算 forecast）

在任何影響時程的事件後重算，寫入 `forecast_start` / `forecast_end`。Baseline 不動。

觸發時機：

- Task 被標記完成（手動或 API）
- Task 的工期、行事曆、依賴被編輯
- Task 被插入或刪除
- 每小時的定期重算（讓「尚未開始但已過計畫開始時間」的 task 預測值隨時間推移）

```python
def forward_pass(tasks, edges, now) -> dict[TaskId, tuple[datetime, datetime]]:
    order = topological_sort(tasks, edges)
    result = {}

    for task in order:                         # 由起點往終點
        if task.is_settled:                    # 見 §4.12，不只 done
            end = task.actual_end or now
            start = task.actual_start
            if start is None and task.status is DONE:
                # 手動勾完成時沒有人「開工」過，actual_start 保持 NULL
                start = calendar_for(task).sub(end, duration_of(task))
            result[task.id] = (start or end, end)
            continue

        preds = edges.predecessors_of(task.id)
        if not preds:
            # 計畫外插入的任務沒有 baseline_start，直接以現在為起點
            earliest = max(now, task.baseline_start or now)
        else:
            # 前置結束時間加上該條邊的 lag，取最晚者
            cal_self = calendar_for(task)
            earliest = max(
                cal_self.add(result[p.id][1], edge.lag_seconds)
                for p, edge in preds
            )

        if task.status == 'running':
            start = task.actual_start           # 已發生的事實，不做對齊
            work_begins = max(now, start)       # 但剩餘工作從現在算起
        else:
            start = cal.next_working_instant(max(earliest, now))
            work_begins = start                 # 不可能在過去開始

        end = cal.add(
            cal.next_working_instant(work_begins),
            remaining_seconds(task),
        )
        result[task.id] = (start, end)

    return result
```

`remaining_seconds(task)`：`running` 中的 task 取 `duration - calendar.elapsed(actual_start, now)`，下限為 0；其餘取完整 `duration`。用 `elapsed` 而非時鐘時間相減，才不會讓跨週末的 `business` task 虛增進度。

**三條容易寫錯的規則**

1. **凡「已結算」的 task 都固定住，不只 `done`。** 一個 `cancelled` 的 task 若仍佔用完整工期，會持續把下游往後推——推的是永遠不會發生的工作。已結算但沒有實際時間戳（開始前就被取消）則收斂為現在的零長度區間。
2. **`running` task 的剩餘工作從 `now` 起算，不是從 `actual_start` 起算。** bar 的起點是實際開工時間（那是事實，不對齊工作時段），但終點必須是「從現在起還要多久」。若從 `actual_start` 加剩餘量，一個已耗盡工期的 task 會算出等於開工時間的結束時間。
3. **手動勾完成的 task 沒有 `actual_start`，不要補。** 直接勾「完成」的人從未經過 `running`，系統並不知道工作何時開始；把 `actual_end` 回填進 `actual_start` 是記下一個不存在的事實，代價很實際：圖上畫出一條零長度的 bar（並把整個 phase 的摘要拉回勾選的那一刻），而 §8.6 的模板健檢會把這一步統計成「耗時 0 秒」。欄位保持 `NULL`，由 forward pass 在顯示時補上它原本被分配的工期。

Case 的 `forecast_end` = 所有 task 的 `forecast_end` 最大值。健康度由 `forecast_end` 與 `target_date` 的關係決定（判定條件見 [design.md §8.1](design.md#81-健康度定義)）。

### 5.4 拓撲排序與環狀偵測

Kahn 演算法。若排序結果的節點數少於總節點數，代表有環；此時以 DFS 找出並回傳**具體的環路節點序列**，供 UI 在流程圖上高亮。

編輯依賴時的前置檢查同樣用這套：新增邊 `(u, v)` 前，先查 `v` 是否已可達 `u`（遞迴 CTE 或記憶體內 DFS）。前端據此把會造成循環的選項從下拉選單中排除。

```sql
-- 查詢 task 的所有下游（用於循環檢查與影響分析）
WITH RECURSIVE downstream AS (
    SELECT successor_id AS id FROM task_dependencies WHERE predecessor_id = :task_id
    UNION
    SELECT d.successor_id FROM task_dependencies d
    JOIN downstream ds ON d.predecessor_id = ds.id
)
SELECT id FROM downstream;
```

### 5.5 Critical Path

以 forecast 結果計算每個 task 的總浮時（total float）：

1. 正推得每個 task 的最早開始 / 結束（即 forward pass 結果）
2. 反推得每個 task 的最晚開始 / 結束（以 **case 自身的 forecast 完成時間** 為錨點，用 forecast 的實際狀態）
3. `float = latest_start - earliest_start`
4. `float <= 0` 者標記 `is_on_critical_path = TRUE`

**錨點是 forecast 完成時間，不是 target_date。** 這點很容易寫錯：若以 target_date 為錨，一個進度超前的 case 會算出「所有 task 都有浮時」，關鍵路徑變成空集合——但 [design.md §4.3](design.md#43-關鍵路徑) 承諾的是「這條路徑上任一 task 延遲，整個 case 就會延後」，那是**圖的性質**，與距離交期還剩多少無關。距離交期還剩多少由緩衝消耗回答（§5.8），兩者互補而非重複。

已結算的 task 不列入關鍵路徑（它們再也不會延誤任何事），`is_optional = TRUE` 的 task 亦不標記（§4.12）。

**注意**：optional 任務不被標記，不代表它的延遲不會傳播。若有必要任務依賴它，延遲照樣沿圖推進到下游——只是不畫上關鍵路徑的強調樣式。這個區別要在 UI 的說明文字中講明，否則使用者會誤以為 optional 等於「不會拖累進度」。

### 5.6 可手算驗證的範例

以使用者原始範例的三個 task（各 12H、線性依賴、`continuous` 行事曆）、目標日期 `2026-08-15 18:00` 為例：

**Backward Pass**

| 順序 | Task | 後繼 | `end` 計算 | `end` | `start = end - 12H` |
|---|---|---|---|---|---|
| 1 | `my_task3` | 無 | = target_date | 08-15 18:00 | 08-15 06:00 |
| 2 | `my_task2` | task3 | = min(task3.start) | 08-15 06:00 | 08-14 18:00 |
| 3 | `my_task1` | task2 | = min(task2.start) | 08-14 18:00 | 08-14 06:00 |

關鍵路徑長度 36 小時，最早需在 **2026-08-14 06:00** 開始。這組數字與 [design.md §3 步驟 3](design.md#3-流程-a從-template-建立-case) 顯示的可行性提示一致。

**Forward Pass**（假設現在是 08-14 20:30，`my_task1` 實際 08-14 06:00→08-14 08:00 完成，`my_task2` 於 08-14 08:30 開始且仍在進行）

| Task | 狀態 | `start` | `end` | vs baseline |
|---|---|---|---|---|
| `my_task1` | done | 08-14 06:00 | 08-14 08:00 | 提前 10H |
| `my_task2` | running | 08-14 08:30 | 08-14 20:30 | 提前 9.5H |
| `my_task3` | pending | 08-14 20:30 | 08-15 08:30 | 提前 9.5H |

Case `forecast_end` = 08-15 08:30 < target_date 08-15 18:00 → 健康度 `on_track`。

若 `my_task2` 實際拖到 08-15 14:00 才結束，則 `my_task3` 預測 08-15 14:00 → 08-16 02:00，超過目標 8 小時 → 健康度 `overdue`。

### 5.7 重算的交易邊界

任何觸發重算的操作在**單一資料庫交易**內完成：

1. `SELECT ... FOR UPDATE` 鎖定該 case 列（序列化同一 case 的並行修改）
2. 載入該 case 的所有 task 與依賴邊
3. 套用變更（更新 task / 插入 task / 標記完成）
4. 執行 forward pass
5. 批次更新 `forecast_*`、`is_on_critical_path`、case 的 `forecast_end` 與 `health`
6. 寫入 `audit_events`
7. 入列需要的 `job_queue` 項目
8. Commit 後才發送 WebSocket 事件與通知

一個 case 的 task 數量級為數十，全量重算的成本遠低於增量演算法的複雜度，因此**一律全量重算**。

**儲存的 forecast 會隨時間變舊。** Forward pass 用 `start = max(earliest, now)` 求值，所以一個尚未開始的任務，其 `forecast_start` 只在重算的那一刻是對的；下一次重算之前，時間繼續往前走，畫面上就會看到一個「預計 10:00 開始」但現在已經 11:30 的任務。定期重算把誤差壓在一小時內，但一小時的誤差在以小時為刻度的 Gantt 上是看得出來的。

處理方式分兩層：

| 用途 | 資料來源 |
|---|---|
| Case 詳情頁的 Gantt | API 回傳前於**記憶體中重跑一次 forward pass**（以當下的 `now`），不寫回資料庫 |
| Case 總表、健康度統計、通知判定 | 直接用資料庫中儲存的欄位 |

單一 case 的 forward pass 是數十個節點的拓撲走訪，在請求路徑上重算的成本可以忽略；而總表要一次列出上百個 case，逐一重算就不划算，容忍一小時的誤差是合理取捨。**兩者用的是同一個純函式**（§5 開頭），不會出現兩套邏輯各算各的。

### 5.8 專案緩衝與緩衝消耗式健康度

反推排程有一個容易被忽略的性質：每個 task 的結束時間**剛好等於**後繼的開始時間，因此**關鍵路徑上零間隙、緊貼交期**。這是一份「每一步都不能出錯」的計畫——任一步延遲一小時，交期就晚一小時。現實中沒有流程能這樣跑。

解法是在模板層宣告**專案緩衝**：

```yaml
gantt:
  template_name: my_template_name
  buffer: 8H          # 反推起點改為 target_date - 8H
```

**緩衝集中放在末端，而不是每個 task 各加保險。** 分散的保險會被帕金森定律吃掉（工作總會填滿可用時間），而且看不出來誰用掉了；集中的緩衝是一塊可量測、可歸因的共享資源。

Backward pass 因此從 `target_date - buffer` 起算（見 §5.2）。緩衝在 Gantt 上是 🎯 之前一段獨立的斜紋區塊，不屬於任何人。

**緩衝消耗式健康度**

有了緩衝之後，健康度就不必再靠「餘裕 < 10%」這種武斷門檻，而能同時看兩個比例：

```python
buffer_consumed_ratio = max(0, forecast_end - plan_deadline) / buffer_seconds
progress_ratio        = 已結算任務工期 / 全部任務工期        # 見 §5.9
```

| 條件 | health |
|---|---|
| `buffer_consumed_ratio <= progress_ratio` | `on_track` — 緩衝消耗速度追得上進度 |
| `progress_ratio < buffer_consumed_ratio <= 1.0` | `at_risk` — 燒緩衝比做事快，但還沒燒完 |
| `buffer_consumed_ratio > 1.0` | `overdue` — 緩衝用罄，預測已超過目標日期 |

這個判定回答的是「**現在該不該緊張**」：做完三成工作卻燒掉七成緩衝，即使預測完成時間仍在交期之內，也該亮黃燈。單純比較「預測完成 vs 目標日期」看不出這件事。

`buffer: 0`（預設）時退回原本的二分判定：`forecast_end > target_date` 即 `overdue`，否則 `on_track`。既有模板不受影響。

### 5.9 工期加權進度

`5/12` 這種任務數比例會把 10 分鐘的通知和 12 小時的測試算成等值。進度一律以**工期加權**計算：

```python
progress_ratio = sum(t.duration_seconds for t in tasks if is_settled(t)) \
               / sum(t.duration_seconds for t in tasks)
```

`running` 中的任務依已耗用時間計入部分權重。UI 同時顯示加權百分比與任務數（`62% · 5/12 完成`）——百分比反映真實工作量，任務數讓使用者知道還有幾件事要處理。

### 5.10 Baseline 的邊界情形

Baseline 是「建立 case 當下的原始計畫」。以下三種情形會讓它與現況脫節，各自的處理方式必須明確定義，否則畫面上會出現無法解釋的落差。

**1. 事後插入的任務**（流程 D）

沒有原始計畫可言，`baseline_start` / `baseline_end` 留 **NULL**。Gantt 對這類任務只畫**單軌**預測 bar，列名旁標「計畫外新增」，偏差欄顯示 `—`。

硬塞一個假 baseline 進去會產生兩個問題：畫出一條從未存在過的計畫 bar，以及算出無意義的偏差數字。留白反而更誠實——而且「這個 case 事後多加了幾件計畫外的工作」本身就是事後檢討時想知道的資訊。

**2. 目標日期被修改**

`PATCH /cases/{id}` 改 `target_date` 時，**baseline 維持不動**——那才是 baseline 的意義。同時做三件事：

- 變更寫入 `target_date_history`（含變更者、時間、備註）
- Gantt 同時畫**原始目標線**（淡色虛線）與**現行目標線**（實線 🎯），兩線之間標註差距
- Case 詳情頁的摘要列標示「交期已調整 2 次」，可展開查看歷程

讓「交期被改過」在畫面上看得見。這通常正是事後檢討時最關鍵、卻最容易被悄悄抹掉的資訊。

**3. 明確重設 baseline**

當一個 case 的計畫已經偏離到 baseline 失去參考價值（例如專案重啟），提供 `POST /cases/{id}/reset-baseline`：以目前的 forecast 覆寫 baseline，**前一份 baseline 完整存入 `baseline_resets`**，寫入稽核紀錄。

限制為 case owner 或 template admin，UI 上需二次確認並說明「原始計畫將移入歷史，偏差數字會歸零」。這是刻意設計得有點麻煩的操作——重設 baseline 等於抹去「我們原本答應了什麼」，不該順手就能做。

---

## 6. Task 執行引擎

### 6.1 Handler Registry

後端以裝飾器註冊處理函式，模組載入時填入全域註冊表：

```python
# app/handlers/backup.py
from app.execution.registry import task_handler, TaskContext, TaskResult

@task_handler("my_function")
class MyFunctionHandler:
    """對應 task_templates.task_api = 'my_function'"""

    async def trigger(self, ctx: TaskContext) -> TaskResult:
        """Task 轉 ready 時呼叫一次。

        ctx.params          展開後的任務參數
        ctx.case_params     case 層級參數
        ctx.task_name       任務名稱
        ctx.idempotency_key 同一 attempt 內固定，供外部系統去重
        """
        job_id = await external_system.submit(ctx.params, key=ctx.idempotency_key)
        return TaskResult.running(external_ref=job_id)

    async def poll(self, ctx: TaskContext) -> TaskResult:
        """trigger_poll / poll_only 模式下依間隔呼叫。"""
        state = await external_system.query(ctx.external_ref)
        if state.done:
            return TaskResult.succeeded(payload=state.output)
        if state.error:
            return TaskResult.failed(message=state.error)
        return TaskResult.running()
```

`TaskResult` 的四種結果：`running`（未完成，繼續等）、`succeeded`（完成）、`failed`（失敗，可重試）、`fatal`（失敗，不重試）。

註冊表在啟動時掃描 `app/handlers/` 套件。`GET /api/v1/handlers` 回傳已註冊清單，供 Task 模板編輯器的下拉選單使用（見 [design.md §9.8](design.md#98-task-模板編輯器)）。

### 6.1.1 內建通用 Handler

自訂 handler 是 Python 程式碼，隨應用程式部署。這代表每接一個新系統都得走「寫程式 → review → 部署」，摩擦大到足以讓使用者乾脆改用手動完成——自動化功能就白做了。

因此內建三個通用 handler，讓多數整合變成**填表單而非寫程式**：

| Handler | 用途 | 設定（存於 task 模板的 `api_config`） |
|---|---|---|
| `http_request` | 呼叫任意 HTTP API | `url`、`method`、`headers`、`body`、`success_when`、`poll_url`、`auth_ref` |
| `wait_for_signal` | 不主動做事，只等外部打 callback | 無（等同 `trigger_callback` 的無觸發版） |
| `shell_command` | 執行白名單內的指令 | `command`、`args`；**預設停用**，需在環境變數明確開啟並設定白名單 |

`http_request` 的設定範例：

```yaml
task:
  id: trigger_build
  task_api: http_request
  api_mode: trigger_poll
  api_config:
    url: "https://ci.internal/api/builds"
    method: POST
    auth_ref: ci_token                        # 指向憑證庫，不在模板中存密鑰
    body:
      project: "${ params.project_code }"
      branch: "${ params.branch }"
    external_ref_path: "$.build_id"           # 從回應取出後續輪詢用的識別碼
    poll_url: "https://ci.internal/api/builds/${ external_ref }"
    success_when: "$.status == 'SUCCESS'"
    failure_when: "$.status in ['FAILED', 'ABORTED']"
```

三項安全約束：

1. **憑證不進模板。** `auth_ref` 指向獨立的憑證庫（`api_credentials` 表，值加密儲存），模板與快照裡只有名稱。否則模板匯出（§8.7）就會把密鑰一起送出去。
2. **URL 白名單。** `http_request` 只能打環境變數 `HTTP_HANDLER_ALLOWED_HOSTS` 列出的主機，防止被當成 SSRF 跳板。
3. **`shell_command` 預設關閉。** 開啟後也只能執行白名單內的指令名稱，參數經 shell 逸出，不經過 `shell=True`。

真正需要複雜邏輯（多步驟交握、特殊協定、資料轉換）時仍寫自訂 handler。內建 handler 的目的是讓**簡單的事情不需要工程資源**，不是取代自訂 handler。

### 6.2 Task 狀態機

```
                    前置全部 done
      ┌─────────┐ ──────────────▶ ┌─────────┐
      │ pending │                  │  ready  │
      └─────────┘ ◀────────────── └─────────┘
           │       前置被改回未完成      │
           │                            │ ┌── 無 task_api → 等待人工
           │                            ▼ ▼
           │                       ┌─────────┐  handler.trigger()
           │                       │ running │ ◀────────────────
           │                       └─────────┘
           │                        │   │   │
           │       succeeded / 手動 │   │   │ failed 且重試未耗盡
           │                        ▼   │   └──────┐
           │                   ┌──────┐ │          │ 退避後
           │                   │ done │ │          ▼ 重新 trigger
           │                   └──────┘ │      (回到 running)
           │                            │ failed 且重試耗盡
           │                            ▼
           │                       ┌────────┐  重新執行 / 手動完成
           │                       │ failed │ ──────────────────▶
           │                       └────────┘
           ▼
      ┌───────────┐
      │ cancelled │   （任何非 done 狀態皆可轉入）
      └───────────┘
```

規則：

- `pending → ready` 由前置 task **結算**時觸發，在同一交易內完成。放行條件是「所有前置 `is_settled`」而非「所有前置 `done`」——見 §4.12 的 `is_settled()` 定義，`cancelled` 與 `on_failure: continue` 的 `failed` 都算結算完畢
- `on_failure: cancel_case` 的 task 失敗時，整個 case 轉 `cancelled`，所有未完成 task 一併取消，佇列中相關工作清除
- `ready → running`：有 `task_api` 者由 worker 觸發；無者由使用者手動完成時直接 `ready → done`
- **手動完成永遠可用**（除非 `allow_manual_override = false`）。若 task 正在 `running`，手動完成會中止等待、將對應的 `task_runs` 標為 `cancelled`，並移除佇列中的輪詢工作。
- `done` 為終態，僅管理員可透過「還原完成」退回（寫入稽核紀錄）
- 已 `done` 或 `running` 的 task 不可刪除，只能 `cancelled`

### 6.3 Worker 迴圈

Worker 是獨立程序，可多份同時執行。主迴圈每秒取件一次：

```sql
UPDATE job_queue
SET locked_by = :worker_id, locked_at = now()
WHERE id IN (
    SELECT id FROM job_queue
    WHERE locked_by IS NULL AND run_after <= now()
    ORDER BY run_after
    LIMIT 10
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

`SKIP LOCKED` 確保多 worker 不會取到同一件。取件後依 `job_type` 分派：

| `job_type` | 行為 |
|---|---|
| `trigger` | 呼叫 `handler.trigger()`。回 `running` 則寫入 `external_ref` 並入列 `poll`；回 `succeeded` 則直接完成 task |
| `poll` | 呼叫 `handler.poll()`。未完成則依 `api_poll_interval` 入列下一次 `poll` |
| `timeout_check` | 檢查 `task_runs.started_at` 是否超過 `api_timeout_s`；是則標記 `timeout` 並依重試策略處理 |
| `recalc` | 執行 forward pass（用於定期重算與延遲重算） |
| `deadline_scan` | 掃描逾期預警、**逾期未開始**與逾期未完成，產生通知（§6.6） |
| `schedule_scan` | 掃描 `template_schedules`，建立到期的週期性 case（§4.15） |

**鎖逾時回收**：`locked_at` 超過 5 分鐘仍未完成的項目視為 worker 崩潰，由清理工作解鎖重新入列。

**排程巡檢**：另有一個每分鐘執行的巡檢工作，掃描 `status = 'ready' AND task_api IS NOT NULL` 但佇列中沒有對應 `trigger` 的 task 並補入列。這是防止事件遺漏的安全網——狀態表始終是唯一真相來源，佇列只是加速器。

### 6.4 冪等性與重試

- 每次 trigger 產生一筆 `task_runs`，`attempt` 遞增，`(case_task_id, attempt)` 唯一
- `idempotency_key` = `sha256(f"{case_id}:{task_id}:{attempt}")`，傳給外部系統供去重
- 重試採**指數退避**：第 n 次重試延遲 `api_retry_interval_s * 2^(n-1)`，上限 1 小時
- 重試次數耗盡後 task 轉 `failed`，發送通知，等待人工介入
- `TaskResult.fatal()` 直接轉 `failed`，跳過剩餘重試

### 6.5 Callback 端點

`trigger_callback` 模式下，觸發時產生一次性 token（存於 `task_runs.request_payload`）。外部系統回報：

```
POST /api/v1/callbacks/{token}
Content-Type: application/json

{ "status": "succeeded", "payload": { "output_path": "/data/out.csv" } }
```

Token 單次有效、綁定特定 `task_run`、隨 `api_timeout` 過期。此端點不需要使用者認證，但驗證 token 有效性、對應 run 仍在 `running`、且未過期。

### 6.6 期限掃描與逾期未開始告警

每 5 分鐘執行的 `deadline_scan` 檢查三種情形：

| 情形 | 判定條件 | 通知對象 |
|---|---|---|
| 即將到期 | `status IN ('ready','running')` 且 `now >= baseline_end - warn_before_seconds` | task owner |
| **逾期未開始** | `status = 'ready'` 且 `now > baseline_start` | task owner + group lead |
| 逾期未完成 | `status IN ('ready','running')` 且 `now > baseline_end` | task owner + case owner |

**「逾期未開始」是三者中唯一真正可行動的訊號。** 等到一個任務的結束時間逾期，事情早就無法挽回；但「該開始卻還沒開始」時，還有機會催人、換人或調整範圍。這條判定所需的資料早就存在表裡，只是原本沒人去比對。

它同時進入 Case 總表的健康度計算（有任務逾期未開始 → 至少 `at_risk`）與「我的任務」的排序權重。

每個收件人對每個任務的每種情形只通知一次（以 §3.9 的 `dedup_key` 去重），避免每 5 分鐘洗版；任務被還原完成或重試時 epoch 遞增，重新進入告警週期。`baseline_start` 為 NULL 的計畫外任務跳過「逾期未開始」判定。

`deadline_scan` 是**唯讀掃描**，只做時間比較與寫入通知，不鎖定 case、不觸發重算。它與 §5.7 的重算交易完全分離，因此每 5 分鐘掃全表也不會影響正在編輯 case 的使用者。

---

## 7. 權限模型

> **用詞區分**：本節的「權限」指存取控制（`is_template_admin`、owner/group 檢查）。DSL 的 `roles`（§4.10）是**模板角色**——流程的人力槽位，建立 case 時綁定實際使用者。兩者是不同概念：被指派為模板角色 `pm` 不會賦予任何額外系統權限，只是讓該使用者成為對應 task 的 owner，再由下方的 owner 規則決定他能做什麼。

### 7.1 認證

Session cookie（`HttpOnly` + `SameSite=Lax` + `Secure`），後端以簽章 session 儲存 `user_id`。密碼以 Argon2id 雜湊。架構上預留 OIDC/SAML 介面（`users.password_hash` 可為 NULL），但首版實作本地帳密。

### 7.2 授權檢查點

| 操作 | 條件 |
|---|---|
| 瀏覽 case / task / template | 任何已登入使用者 |
| 建立 case | 任何已登入使用者 |
| 編輯 case 屬性（名稱、目標日期） | case owner 或 template admin |
| 重新指派模板角色（批次改該角色的所有 task owner） | case owner 或 template admin |
| 取消 / 刪除 case | case owner 或 template admin |
| 編輯 task（工期、參數、依賴） | task owner **或** task 所屬 group 成員 **或** case owner |
| 標記 task 完成 | task owner **或** task 所屬 group 成員 |
| 插入 / 刪除 task | task owner、相鄰 task 的 group 成員、或 case owner |
| 重新執行失敗的 API | task owner 或 group 成員 |
| 建立 / 編輯 / 發布 Gantt 模板 | `is_template_admin` |
| 建立 / 編輯 Task 模板 | `is_template_admin` |
| 管理行事曆 | `is_template_admin` |
| 管理使用者與群組 | `is_template_admin` |

實作為單一服務層函式：

```python
def can_complete_task(user: User, task: CaseTask) -> bool:
    if task.owner_id == user.id:
        return True
    if task.group_id and user_in_group(user.id, task.group_id):
        return True
    return False
```

**「同群組可代打」是刻意的設計**：現實中負責人請假時流程不該卡住。所有代打行為完整記錄於 `audit_events`，`completed_by` 欄位保留真正的操作者。

### 7.3 前端呈現

API 在回傳 case/task 時附帶 `permissions` 物件（`{"can_edit": true, "can_complete": false, ...}`），前端據此決定按鈕是否可用。**前端只負責呈現，所有授權判斷以後端為準**，前端的判斷純粹是 UX 優化。

---

## 8. REST API

Base path：`/api/v1`。所有回應為 JSON，錯誤格式統一：

```json
{
  "error": { "code": "E_CYCLE", "message": "依賴循環：my_task1 → my_task2 → my_task1",
             "details": { "cycle": ["my_task1", "my_task2", "my_task1"] } }
}
```

### 8.1 端點清單

**認證**

```
POST   /auth/login                  登入
POST   /auth/logout                 登出
GET    /auth/me                     目前使用者與權限
```

**Gantt 模板**

```
GET    /templates                          列表（含 status 篩選）
GET    /templates/{name}                   最新已發布版本
GET    /templates/{name}/versions          版本歷史
GET    /templates/{name}/versions/{v}      指定版本
GET    /templates/{name}/diff?from=3&to=4  版本差異
POST   /templates                          建立草稿
PUT    /templates/{name}/draft             更新草稿
POST   /templates/{name}/validate          驗證（不儲存），回傳 errors + warnings
POST   /templates/{name}/publish           發布為新版本
POST   /templates/{name}/preview           試算預覽（見 8.3）
DELETE /templates/{name}/draft             捨棄草稿
GET    /templates/{name}/health            模板健檢報表（見 8.6）
GET    /templates/{name}/export            匯出 YAML（見 8.7）
POST   /templates/import                   匯入 YAML
GET    /templates/{name}/schedule          週期建立設定（§4.15）
PUT    /templates/{name}/schedule
DELETE /templates/{name}/schedule
POST   /templates/{name}/schedule/run-now  立即依排程設定建立一個 case
```

**Task 模板**

```
GET    /task-templates
POST   /task-templates
GET    /task-templates/{name}
PUT    /task-templates/{name}
DELETE /task-templates/{name}       僅在未被任何模板引用時允許
GET    /handlers                    已註冊的 task_api 清單與狀態
```

**Case**

```
GET    /cases                       列表；支援 status/health/template/owner/group/
                                    q/target_before/target_after/sort/page
POST   /cases                       建立（見 8.2）
POST   /cases/preview               建立前預覽時程（不落地）
GET    /cases/{id}                  含 tasks、dependencies、permissions
PATCH  /cases/{id}                  更新名稱 / 目標日期（會觸發重算）
PUT    /cases/{id}/roles            重新指派模板角色，批次更新對應 task 的 owner
                                    （僅更新 owner 仍由該角色推導、未被個別覆寫的 task）
POST   /cases/{id}/reset-baseline   以目前 forecast 覆寫 baseline（§5.10），需二次確認
POST   /cases/{id}/archive          封存（§10.2）
POST   /cases/{id}/cancel
DELETE /cases/{id}
GET    /cases/{id}/audit            稽核紀錄
GET    /cases/{id}/export           CSV
GET    /cases/summary               總表頂部的健康度統計
```

**Case Task**

```
PATCH  /cases/{id}/tasks/{task_id}            編輯（工期/owner/參數/依賴）
POST   /cases/{id}/tasks/{task_id}/complete   手動標記完成
POST   /cases/{id}/tasks/{task_id}/reopen     還原完成（僅 admin）
POST   /cases/{id}/tasks/{task_id}/retry      重新執行失敗的 API
POST   /cases/{id}/tasks/insert               插入新 task（見 8.4）
DELETE /cases/{id}/tasks/{task_id}            刪除
POST   /cases/{id}/tasks/simulate             模擬變更的影響（見 8.5）
GET    /cases/{id}/tasks/{task_id}/runs       API 執行紀錄
```

**個人**

```
GET    /my/tasks?filter=actionable|running|done
GET    /notifications
POST   /notifications/{id}/read
POST   /notifications/read-all
```

**外部回呼**

```
POST   /callbacks/{token}           不需登入，token 驗證
```

### 8.2 建立 Case

```http
POST /api/v1/cases
{
  "name": "2026Q3 新產品導入 - 型號 A",
  "template_name": "my_template_name",
  "template_version": 3,
  "params": { "my_para1": 3, "my_para2": "test", "line_type": "A" },
  "role_assignments": { "pm": 17, "qa_lead": 23 },
  "target_date": "2026-08-15T18:00:00+08:00",
  "idempotency_key": "c7f3a1e2-..."
}
```

`idempotency_key` 由前端在開啟建立精靈時產生一次，整個精靈流程沿用同一組。重複送出（連點、網路重試、使用者按上一步再送一次）時後端偵測到既有的 key，直接回傳原本建立的 case 而非再建一個。以 `gantt_cases.idempotency_key` 的唯一索引保證，不需額外的鎖。

服務層流程：

1. 載入指定版本模板；驗證參數符合 `template_para` schema，且所有 `required: true` 的角色皆已指派
2. 執行 §4.14 的建立期展開管線（`when` 過濾、運算式求值、依賴與 owner 解析、圖驗證）
3. 建立自我包含的快照（§4.8），並記錄 `role_assignments` 與 `skipped_tasks`
4. 建立 `case_tasks` 與 `task_dependencies`（含 `lag_seconds`）
5. 執行 backward pass 寫入 `baseline_*`
6. 執行 forward pass 寫入 `forecast_*`、`health`
7. 將無前置的 task 設為 `ready`，其餘 `pending`
8. 入列有 `task_api` 的 `ready` task 的 `trigger` 工作
9. 寫入 `case.created` 稽核紀錄，發送指派通知

全程單一交易。

### 8.3 預覽（不落地）

`POST /cases/preview` 與 `POST /templates/{name}/preview` 共用同一段排程邏輯，但只執行上述步驟 1–6 的**記憶體版本**，直接回傳計算結果：

```json
{
  "tasks": [
    { "name": "my_task1", "display_name": "需求確認", "phase": "準備階段",
      "baseline_start": "2026-08-14T06:00:00+08:00",
      "baseline_end":   "2026-08-14T18:00:00+08:00",
      "owner": "王小明", "group": "研發部", "is_on_critical_path": true },
    { "name": "functional_test", "display_name": "功能測試", "phase": "測試階段",
      "...": "..." }
  ],
  "dependencies": [ { "from": "my_task1", "to": "batch_test_0", "lag_seconds": 14400 } ],
  "skipped_tasks": [
    { "id": "safety_review", "label": "安規審查", "reason": "when 條件為 false" }
  ],
  "critical_path_seconds": 129600,
  "earliest_start": "2026-08-14T06:00:00+08:00",
  "feasible": true,
  "slack_seconds": 1382400,
  "warnings": []
}
```

`skipped_tasks` 讓管理員在試算時立刻看到「這組參數會略過哪些步驟」，這是驗證含 `when` 模板的主要手段（§4.7）。

這支 API 同時支撐建立精靈的步驟 3 可行性提示、步驟 4 的 Gantt 預覽、以及模板編輯器的試算預覽。**一份邏輯，三個使用場景。**

### 8.4 插入 Task

```http
POST /api/v1/cases/42/tasks/insert
{
  "task_template": "tt4",
  "name": "supervisor_review",
  "display_name": "主管審核",
  "owner_id": 17,
  "group_id": 4,
  "duration": "4H",
  "params": {},
  "predecessors": ["my_task1"],
  "successors": ["my_task2"],
  "mode": "serial"          // serial：切斷 my_task1→my_task2 並串接
                            // parallel：保留原邊，新 task 只掛在 predecessors 之後
}
```

`serial` 模式下，服務層會移除 `predecessors × successors` 之間的直接邊，改為 `pred → new → succ`。`parallel` 模式保留原有的邊。

插入後全量重算 forecast，回傳新的完整 case 狀態。

### 8.5 模擬（Dry-run）

支撐 [design.md §5](design.md#5-流程-c編輯-task) 的「即時影響預估」。請求體與 `PATCH` / `insert` 相同，但不寫入資料庫，回傳套用後的預測結果與差異：

```json
{
  "current":  { "forecast_end": "2026-08-15T08:30:00+08:00", "health": "on_track" },
  "simulated":{ "forecast_end": "2026-08-16T02:00:00+08:00", "health": "overdue" },
  "delta_seconds": 62400,
  "affected_tasks": [
    { "name": "my_task3", "delta_seconds": 62400 }
  ],
  "exceeds_target": true,
  "exceeds_target_by_seconds": 28800
}
```

### 8.6 模板健檢報表

系統已經逐筆記錄每個 task 的 baseline 與 actual。同一個 task 模板跑過數十次之後，這批資料就能回答一個模板管理員原本只能靠感覺的問題：**我們寫的工期估得準嗎？**

```http
GET /api/v1/templates/my_template_name/health?since=2026-01-01
```

```json
{
  "case_count": 47,
  "on_time_ratio": 0.72,
  "avg_buffer_consumed": 0.61,
  "tasks": [
    {
      "task_id": "my_task2",
      "label": "測試驗證",
      "planned_duration_seconds": 43200,
      "sample_size": 47,
      "actual_median_seconds": 57600,
      "actual_p80_seconds": 79200,
      "overrun_ratio": 0.68,
      "on_critical_path_ratio": 0.91,
      "suggestion": "建議將預設工期由 12H 調整為 16H（中位數）或 22H（P80）"
    }
  ],
  "bottlenecks": [
    { "task_id": "my_task2", "reason": "91% 的 case 中位於關鍵路徑，且 68% 超出計畫工期" }
  ]
}
```

實作為對 `case_tasks` 的聚合查詢（依 `source_task_template` 與 `name` 分組），資料量到達瓶頸時再改為每日更新的 materialized view。只納入 `status = 'done'` 且有 `actual_start` / `actual_end` 的任務，`cancelled` 與計畫外插入的不計入。

**這是資料模型免費附贈的功能**——不需要任何額外的資料收集，只是沒人特地去查。但它讓模板從「一次寫好就不再管」變成可以持續校準的東西，也是唯一能回答「我們的流程到底哪一步在拖」的地方。

Template 編輯器在該模板的任一任務出現顯著低估（中位數超出計畫 20% 以上、樣本數 ≥ 10）時，於工期欄位旁顯示提示與一鍵套用建議值。

### 8.7 模板匯出與匯入

模板本來就是 YAML，提供匯出匯入幾乎沒有成本，但換來三件事：進 git 版控、在測試與正式環境之間搬移、以及災難復原。

```
GET  /templates/{name}/export?version=3&include_task_templates=true
POST /templates/import
```

匯出為單一 YAML 檔，可選擇是否一併包含所引用的 task 模板與行事曆定義（預設包含，讓檔案自我完備）。

匯入行為：

- 一律匯入為**草稿**，絕不直接覆寫已發布版本
- 匯入前先跑完整驗證（§4.7），失敗則回報錯誤且不寫入任何東西
- 名稱已存在時，比對差異並要求使用者確認是「建立新草稿」或「另存為新模板名稱」
- 所引用的 task 模板不存在時一併建立；已存在但定義不同時，明確列出差異讓使用者選擇沿用現有或覆寫
- **憑證不隨模板走**：`auth_ref`（§6.1.1）只匯出名稱，目標環境必須自行設定同名憑證。匯入時若找不到對應憑證，以警告形式列出而非阻擋——讓使用者先把流程搬過去，再補設定。

### 8.8 WebSocket

```
WS /ws/cases/{id}
```

連線後訂閱該 case 的變更。伺服器推送事件：

```json
{ "type": "task.status_changed", "task_id": 128, "status": "done",
  "case": { "forecast_end": "...", "health": "on_track" },
  "affected_tasks": [ { "id": 129, "status": "ready", "forecast_start": "..." } ] }
```

事件類型：`task.status_changed`、`task.updated`、`task.inserted`、`task.deleted`、`case.updated`、`run.progress`。

前端收到事件後直接更新 TanStack Query 快取，不重新請求整包資料。連線中斷重連後拉一次完整狀態以校正。

---

## 9. 前端架構

### 9.1 目錄結構

```
web/src/
├─ api/                    OpenAPI 生成的 client + TanStack Query hooks
├─ components/
│  ├─ gantt/
│  │  ├─ GanttChart.tsx       容器：捲動、縮放、虛擬化
│  │  ├─ TimeAxis.tsx         時間刻度（小時/天/週）
│  │  ├─ TaskRow.tsx          單列：baseline bar + forecast bar
│  │  ├─ DependencyLayer.tsx  依賴曲線（含插入按鈕熱區）
│  │  ├─ Markers.tsx          今日線、目標日期線
│  │  └─ useGanttLayout.ts    時間 ↔ 像素換算、佈局計算
│  ├─ template-editor/
│  │  ├─ FormMode.tsx
│  │  ├─ FlowMode.tsx         React Flow
│  │  ├─ YamlMode.tsx         Monaco
│  │  ├─ ValidationPanel.tsx
│  │  └─ useTemplateModel.ts  三模式共用的單一資料源
│  └─ common/
├─ features/
│  ├─ case-list/  case-detail/  case-create/  my-tasks/  notifications/
├─ stores/                 Zustand：縮放、篩選、drawer
└─ lib/                    duration 解析、時間格式化、色彩對照
```

### 9.2 為何自建 Gantt 渲染

評估過 frappe-gantt、React Flow 的時間軸擴充、以及商業套件後選擇自建，理由：

1. **雙軌 bar** — 每個 task 需要 baseline 與 forecast 兩條可獨立設定樣式的 bar，現成套件普遍假設「一個 task 一條 bar」
2. **DAG 依賴** — 多前置匯流的連線佈局需要客製，多數輕量套件只支援線性或單一前置
3. **狀態紋理** — 六種狀態 × 逾期修飾的視覺組合需要完全控制 SVG 輸出
4. **互動需求** — 依賴線上的插入按鈕、hover 上下游高亮，都需要對渲染層有完整掌控
5. **無授權成本** — 商業套件的授權與客製限制不划算

實作核心是 `useGanttLayout`：給定時間範圍與容器寬度，回傳 `timeToX(t)` 與 `xToTime(x)` 換算函式。所有元件基於這兩個函式繪製 SVG，縮放時只需改變換算參數而不重建 DOM。

任務列採**虛擬化**（僅渲染可視範圍 ±10 列），確保上百個 task 的 case 仍然流暢。

### 9.3 Template 編輯器三模式同步

三種模式共用 `useTemplateModel` 持有的單一 `TemplateModel` 物件（記憶體中的正規化結構）：

```
                    ┌──────────────────┐
      表單模式 ────▶ │  TemplateModel   │ ◀──── 流程圖模式
                    │  (正規化物件)     │
      YAML 模式 ───▶ └──────────────────┘
                             │
                             ▼
                    驗證器（§4.7）→ ValidationPanel
```

- 表單與流程圖模式**直接操作** `TemplateModel`
- YAML 模式在編輯時解析為 `TemplateModel`；解析失敗則保留最後一次成功的模型，並在編輯器內標示錯誤位置。此時切換到其他模式會被阻擋並提示先修正語法。
- 由 `TemplateModel` 序列化回 YAML 時保留欄位順序與註解（使用 `yaml` 套件的 AST 模式），避免使用者在表單模式改一個欄位就把整份 YAML 重排

驗證在每次模型變更後以 300ms debounce 執行。結構性驗證（循環、參照）在前端本地完成以求即時；涉及後端狀態的驗證（`W_UNREGISTERED_API`、`E_UNKNOWN_TASK_TEMPLATE`）呼叫 `POST /templates/{name}/validate`。

---

## 10. 一致性、並行與資料保留

### 10.1 一致性與並行

| 議題 | 處理方式 |
|---|---|
| 同一 case 的並行修改 | 修改前 `SELECT ... FOR UPDATE` 鎖定 case 列，序列化所有變更 |
| 樂觀鎖 | `gantt_cases.version` 與 `case_tasks.version`；請求帶入讀取時的版本，不符則回 409 並提示重新整理 |
| 快照不可變 | `template_snapshot` 無任何更新路徑；DB 層以 trigger 阻擋 UPDATE |
| 已發布模板不可變 | 服務層檢查 `status = 'published'` 時拒絕 UPDATE |
| 重複完成 | `complete` 端點檢查目前狀態；已 `done` 則回 409 而非重複執行連鎖反應 |
| API 重複觸發 | `job_queue` 入列前檢查同 task 是否已有未完成的 `trigger`；加上 `idempotency_key` 雙重保險 |
| 手動完成與 API 完成競合 | 兩者都在鎖定 case 的交易內執行；先到者勝，後到者發現狀態已是 `done` 便放棄並記錄稽核 |
| Worker 崩潰 | `locked_at` 逾時回收 + 每分鐘的排程巡檢補件 |
| 時區 | 全部以 `TIMESTAMPTZ` 儲存 UTC；行事曆自帶時區；前端依使用者瀏覽器時區顯示 |

### 10.2 資料保留與封存

三張表會無限成長，需要明確的保留策略，否則兩年後 Case 總表的查詢會開始變慢：

| 資料 | 策略 |
|---|---|
| `gantt_cases` | 完成或取消滿 90 天自動設 `archived_at`。封存的 case **不刪除**，只是從預設清單與統計中排除；篩選器可切換「包含已封存」 |
| `case_tasks` / `task_dependencies` | 隨 case 一起封存，永久保留——這是 §8.6 模板健檢的資料來源，刪掉就失去校準能力 |
| `audit_events` | 保留 2 年後轉存為每月一份的壓縮 JSONL 檔（物件儲存或本機磁碟），資料庫內只留摘要 |
| `task_runs` | 保留 1 年；`response_payload` 與 `error_detail` 滿 90 天後清空（這兩欄最占空間且事後價值最低），其餘欄位保留 |
| `notifications` | 已讀滿 90 天刪除；未讀保留 |
| `job_queue` | 完成即刪除 |

由每日的維護工作執行，各項天數皆為環境變數可調。封存與清理都寫入 `audit_events`，避免資料「莫名其妙消失」。

### 10.3 搜尋範圍

Case 總表的 `q` 參數搜尋三個範圍，以 PostgreSQL 全文檢索實作：

1. **Case 名稱**（權重最高）
2. **任務名稱與顯示名稱**——「哪個 case 有做過安規審查」是實際會問的問題
3. **參數值**中的字串型參數——例如用料號或批號找出相關 case

以 `gantt_cases` 上的 `search_vector tsvector` 欄位承載，由 trigger 在 case 建立與 task 變更時更新，加 GIN 索引。不搜尋備註與稽核紀錄——那些內容雜訊高、命中率低，會稀釋結果品質。

---

## 11. 測試策略

### 11.1 排程引擎（最高優先）

引擎是系統正確性的核心，以**黃金測資表**驅動：每筆測資為 `(tasks, edges, calendars, target_date) → 期望的每個 task 起訖時間`，以 YAML 定義，pytest 參數化執行。

必須涵蓋的案例：

- §5.6 的三段線性流程（可手算驗證）
- 菱形 DAG：`A → {B, C} → D`，B 與 C 工期不同，驗證 D 的開始時間取最晚的前置
- 多終點：兩條並行分支各自對齊 `target_date`
- 混合行事曆：同一 case 內 `continuous` 與 `business` task 交錯
- 工作日曆跨週末：週五 16:00 起算 4 小時工期，應落在下週一
- 工作日曆跨假日
- 零工期 task
- 單一 task 無依賴
- 環狀依賴 → 拋出 `CycleError` 並回傳正確的環路序列
- Forecast：前置提前完成 / 延後完成 / 進行中且已超時
- Critical path：菱形 DAG 中只有較長的分支被標記
- **Lag**：backward 與 forward 兩個方向的 lag 換算；`business` 行事曆下的 lag 跨越週末
- **Optional**：optional 任務不被標記關鍵路徑，但其延遲仍推遲下游必要任務
- **緩衝**：`buffer: 0` 與 `buffer: 8H` 對同一組輸入的 baseline 差異恰為 8 小時
- **緩衝消耗健康度**：進度 30% 而緩衝消耗 70% → `at_risk`；緩衝消耗 > 100% → `overdue`
- **加權進度**：工期差異懸殊的任務組合，加權比例與任務數比例明顯不同
- **Baseline 為 NULL**：計畫外插入的任務參與 forward pass 但不產生偏差數字

### 11.2 執行引擎

- Handler 契約測試：以假 handler 驗證 `TaskResult` 四種結果的狀態轉換
- **內建 handler**：`http_request` 對假伺服器的觸發與輪詢、`success_when` / `failure_when` 判定、URL 白名單拒絕未列出的主機、憑證不出現在快照與匯出檔中
- **期限掃描**：逾期未開始的判定邊界，以及同一任務不重複發送通知
- **週期建立**：cron 到期建立、重複執行不產生第二個 case、錯過的執行不補建
- 重試退避時序
- 逾時偵測
- 冪等性：同一 `attempt` 重複執行不產生兩筆 `task_runs`
- 多 worker 並行取件不重複（以真實 PostgreSQL 執行）
- Worker 崩潰後的鎖回收

### 11.3 DSL 與驗證

- **使用者原始範例的 YAML 可原封不動解析成功**（回歸保護）；正式名稱與舊別名兩種寫法產生完全相同的內部結構
- §4.7 每個錯誤碼與警告碼各一筆測資
- 運算式求值器的白名單測試：確認 `__import__`、屬性存取、下標、推導式等被拒絕
- **展開管線**（§4.14）：以「同一模板 × 多組參數 → 期望的節點集合與邊集合」為測資形式
  - `when` 為 false 時的 bypass 接回，含連續多個節點被略過的遞移情形
  - 被略過節點兩側的 lag 正確相加
  - 菱形分支中略過一側後，另一側與匯流點的依賴仍然正確
  - `owner` 五種寫法的解析，含 `same_as` 鏈式引用與循環偵測
  - 展開結果的決定性：同一組輸入重複執行產生完全相同的圖

### 11.4 API 與 E2E

- API 層：授權矩陣（§7.2 每一列各測「有權」與「無權」兩種）、樂觀鎖衝突、409 情境
- E2E（Playwright）：
  1. 建立模板 → 發布 → 建立 case → 檢視 Gantt
  2. 標記 task 完成 → 下游轉 ready → 預測時間更新
  3. 在兩 task 間插入新 task → 確認時程延長
  4. API task 失敗 → 重試 → 手動完成
  5. 模板改版 → 確認既有 case 不受影響

---

## 12. 部署與維運

### 12.1 部署單元

實作於 [docker-compose.yml](docker-compose.yml)：

```yaml
services:
  db:       postgres:15-alpine        # 持久化 volume + healthcheck
  migrate:  alembic upgrade head      # 一次性，其餘服務等它成功才啟動
  api:      uvicorn app.api.main:app --workers 4
  worker:   python -m app.execution.worker   # 可 scale 多份
  web:      nginx 提供 web/dist + 反向代理 /api
```

`api` 與 `worker` 共用同一份映像，只是指令不同——兩個程序跑的是同一份程式碼，分開建置只會製造走鐘的機會。

**遷移是獨立的一次性服務**，而不是塞在 entrypoint 裡：後者會讓 api 與 worker 同時對同一個資料庫跑 alembic。`api` 與 `worker` 都以 `service_completed_successfully` 等它。

映像以非 root 使用者執行。`SESSION_SECRET` 沒有預設值，compose 在未設定時直接拒絕啟動，因為它同時是 session 簽章金鑰與憑證加密金鑰的來源。

### 12.2 設定

| 環境變數 | 說明 |
|---|---|
| `DATABASE_URL` | PostgreSQL 連線字串 |
| `SESSION_SECRET` | Session 簽章金鑰 |
| `SMTP_*` | Email 通知設定 |
| `WORKER_ID` | Worker 識別（預設為 hostname + pid） |
| `WORKER_POLL_INTERVAL_MS` | 取件間隔，預設 1000 |
| `DEFAULT_TIMEZONE` | 預設時區，`Asia/Taipei` |
| `HTTP_HANDLER_ALLOWED_HOSTS` | `http_request` 內建 handler 可連線的主機白名單（§6.1.1） |
| `SHELL_HANDLER_ENABLED` | `shell_command` 是否啟用，預設 `false` |
| `SHELL_HANDLER_ALLOWED_COMMANDS` | 啟用時的指令白名單 |
| `CREDENTIAL_ENCRYPTION_KEY` | `api_credentials` 的加密金鑰 |
| `NOTIFICATION_CHANNELS` | 啟用的外送管道，如 `email,slack` |
| `RETENTION_*` | 各項保留天數（§10.2），皆有預設值 |

### 12.3 可觀測性

- **結構化日誌**（JSON），每筆帶 `case_id` / `task_id` / `run_id` 以利追蹤單一流程的完整生命週期
- **指標**：`job_queue` 待處理數與最舊項目年齡、handler 執行時間分佈與失敗率、重算耗時、逾期 case 數
- **健康檢查**：`GET /healthz`（存活）與 `GET /readyz`（DB 可連線 + worker 心跳正常）
- **告警**：佇列積壓超過門檻、worker 心跳中斷、handler 失敗率異常

---

## 13. 實作路線

分六階段，每階段結束時都有可展示的成果。

| 階段 | 內容 | 產出 |
|---|---|---|
| **1. 基礎與 DSL** | 資料模型、Alembic 遷移、DSL 解析與驗證器、別名相容層、受限運算式求值器、展開管線（`when` / owner 解析）、認證與權限 | 可用 CLI 對一份模板 YAML 加一組參數印出展開後的節點與邊 |
| **2. 排程引擎** | Calendar 兩種實作、backward/forward pass（含 lag 與緩衝）、拓撲排序、critical path、加權進度、緩衝消耗健康度、黃金測資 | 引擎單元測試全綠，可手算驗證 |
| **3. Case 核心 API** | 建立 case（含角色指派與冪等鍵）、快照、預覽 API、case 列表與搜尋、task 編輯、手動完成、`on_failure` 結算規則、baseline 邊界情形 | Postman/curl 可跑完整流程 |
| **4. Gantt 視覺化** | React 骨架、Gantt 渲染層（含緩衝區塊與單軌計畫外任務）、Case 總表、Case 詳情、建立精靈、我的任務 | 使用者可完整操作「建立 → 檢視 → 手動完成」 |
| **5. 執行引擎** | Handler registry、**內建通用 handler**、job_queue、worker、重試與逾時、callback 端點、期限掃描與逾期未開始告警、WebSocket 推送、通知管道 | 自動完成的 task 可端到端運作，且不需寫程式就能接 HTTP API |
| **6. Template 編輯器與營運工具** | 三模式編輯器、驗證面板、試算預覽、版本與 diff、Task 模板編輯器、匯出匯入、週期建立、模板健檢報表 | 模板管理員不需再手改 YAML 檔，且能依實際資料校準工期 |

### 13.1 實作進度

| 階段 | 狀態 |
|---|---|
| 1. 基礎與 DSL | 完成 |
| 2. 排程引擎 | 完成 |
| 3. Case 核心 API | 完成 |
| 4. Gantt 視覺化 | 完成（自建 SVG 雙軌 bar、DAG 連線、緩衝區塊、phase 分組） |
| 5. 執行引擎 | 完成 |
| 6. Template 編輯器與營運工具 | 後端完成；UI 為表單（唯讀）＋原始碼兩模式，**流程圖模式尚未實作** |

**已知較薄的部分**

- **流程圖編輯模式**：design.md §9.1 描述三模式，目前只有表單與原始碼兩種。流程圖需要 React Flow 之類的畫布，且是三者中唯一「缺了仍能編模板」的。
- **表單模式唯讀**：可讀可看，編輯要切到原始碼模式。
- **WebSocket 即時推送**（§8.8）：尚未實作。前端靠 TanStack Query 的 staleTime 與視窗聚焦重取，通知鈴則每分鐘輪詢——worker 的掃描本來就是五分鐘一次，輪詢更快也不會有新消息。
- **Gantt 列虛擬化**：`visibleRows` 已實作並測試，但 `GanttChart` 目前一次渲染全部列；任務數上百時要接上。
- **依賴線上的插入按鈕**（design.md §6 的觸發方式之一）：目前從工具列的「+ Insert step」進入，尚未做成點依賴線中點的 `⊕`。

階段 1–3 是後端骨幹，階段 4 讓系統可被實際使用，階段 5–6 補上自動化與自助管理。若需要更早交付，階段 4 結束時的系統已可作為「手動回報版」上線試用。

模板健檢（§8.6）雖列在階段 6，但它依賴的是階段 3 就開始累積的資料——**只要階段 3 上線後有人在用，資料就在長**。這是把它排在最後也不會來不及的原因。

---

## 14. 待決議項目

以下項目在首版採取列出的預設做法，實際使用後再依需求調整：

| 項目 | 首版做法 | 未來可能方向 |
|---|---|---|
| 依賴類型 | 僅 FS + 非負 lag | 加入 SS / FF（負 lag 的正解） |
| `requirement` 語義 | AND（全部完成） | 加 `requirement_mode: any` |
| Task 間資料流 | 無；task 之間不傳遞執行結果 | task 模板宣告 `outputs`，下游以 `${ tasks.x.outputs.y }` 執行期引用；手動完成的對話框依 outputs schema 生成結構化表單 |
| 中間時間錨點 | 只有末端一個 `target_date` | `type: milestone` + 節點層級 `deadline`，backward pass 改為 `end = min(後繼 start, 自身 deadline, target_date)`；需同時處理「錨點互相衝突」的驗證與提示 |
| 同型任務展開 | 不支援；平行的同型步驟逐一寫出 | 若日後出現「同一步驟要跑 N 份」的流程，再評估 `for_each` 之類的展開語法 |
| 執行期分支 | `when` 僅建立期求值；返工類需求以 optional + 手動啟用替代 | 需要重新定義 baseline 的語義才能支援 |
| 模板組合 | 不支援 `extends` 與子流程巢狀 | 名稱空間、跨層依賴、快照展開策略都需先想清楚 |
| 資源衝突 | 不檢查（同一人可同時被排多個 task） | 資源負載視圖與衝突警示；模板健檢（§8.6）累積的資料可用來判斷是否真的需要 |
| 模板升級 | 既有 case 完全隔離 | 提供 diff 與選擇性套用 |
| 通知管道 | 站內 + Email；管道介面已可插拔（§3.9） | Slack / Teams / LINE 各補一個實作檔即可 |
| 認證 | 本地帳密 | OIDC / SAML SSO |

「Task 間資料流」與「中間時間錨點」兩項已完成設計評估，暫不納入首版；若之後要補，`${ }` 與 `{{ }}` 的語法分工、以及 backward pass 的錨點規則已在上表定調，不需重新設計。
