# Persistent Reconciler 设计文档(2026-08-01)

> **状态**:Draft,待用户 review。
> **目标**:把 memorial 从"无状态 webhook handler + 内存 queue"升级为"有状态 reconciler",
> 解决 2026-07-31 线上问题清单 #1 / #3 / #4,并附带修复 #2 / #5。
> **不在本设计范围**:Hindsight 上游的 `data={}` 文档 ID 漏洞、API 配额管理、
> 多 poller 并发、跨进程去重容器化、记忆内容加密。

---

## 1. 背景与动机

2026-07-31 线上日志(`doc/webhook-runtime-findings-2026-07-31.md`)暴露了当前实现
的几条相互纠缠的问题:

| # | 问题 | 根因 |
|---|---|---|
| 1 | 同一 `document_id` 的多次 webhook 触发累计 units 重复 reflect | 没有跨 webhook 的 unit_id 持久去重 |
| 2 | structured verdict 为空时,reasoning 中的 UUID 被误清理 | `reflect_query.py` 不该回退到自然语言扫 UUID |
| 3 | 单个 reflect 超时中断整批 | 异常从单条 unit 冒出导致 `handle_event` 中断循环 |
| 4 | 大 document 长时间占用单 worker | 没有"分片 + 状态化"处理 |
| 5 | `include_based_on` 被 Hindsight 忽略 | API 契约漂移 |

**核心架构调整**:把"webhook 一次性 reconcile 整批 units"拆成两段流水线:

- **webhook 线程**:签名校验 → 拉 units → **落库**(仅此)→ 返回 200;
- **poller 线程**:常驻,从本地表里按 `created_at DESC` 一次取一行 → reflect + curate → 标完成。

这两条流水线**不共享内存状态**,只通过数据库表交互,因此:
- 跨 webhook 重复(unit 级)天然被本地表唯一键拦掉;
- 进程崩溃不会丢 pending 行(行在 DB 里,重启后 poller 会扫);
- 单元级失败不影响同一 document 其他 unit;
- 整批"超时/中断"在架构层不可能发生。

---

## 2. 架构总览

### 2.1 模块依赖

```
hindsight_memorial/
├── webhook_server.py      # 改:main() 增加 init_db() + poller.start()/stop()
├── webhook_handlers.py    # 改:去掉 dispatcher 入队,改为 sync 落库;复用主/ fallback 共用落库函数
├── reconcile.py           # 改:签名 run_reconcile(bank_id, unit_id, content) 替代事件整体
├── reflect_query.py      # 改:不再回退扫 UUID(structured_only=True 默认)
├── curate.py              # 改:除了 Hindsight 端 PATCH,同步把本地命中行标 superseded
├── client.py              # 不动
├── config.py              # 改:加 MySQL 连接配置
├── db.py                  # 新:DB 连接、schema 初始化、CRUD 辅助
├── poller.py              # 新:ReconcilerPoller 类
└── (dispatch.py           # 删除)
```

### 2.2 数据流

```
┌─────────────────┐  HTTP POST            ┌──────────────────────┐
│   Hindsight     │ ────────────────────► │  webhook_server      │
│   (webhook)     │                       │  ThreadingHTTPServer │
└─────────────────┘                       └──────────┬───────────┘
                                                      │ thread-per-request
                                                      ▼
                                          ┌──────────────────────┐
                                          │  webhook_handlers    │
                                          │  _process_post       │
                                          └──────────┬───────────┘
                                                      │ sync 落库
                                                      ▼
                                          ┌──────────────────────┐
                                          │  db.upsert_units     │
                                          │  (per webhook 提交)   │
                                          └──────────┬───────────┘
                                                     │ INSERT/UPDATE
                                                     ▼
                                          ┌──────────────────────┐
                                          │  MySQL              │
                                          │  memory_units 表    │
                                          └──────────┬───────────┘
                                                     │ SELECT pending
                                                     ▼
                                          ┌──────────────────────┐
                                          │  poller              │
                                          │  ReconcilerPoller    │
                                          │  (daemon thread)     │
                                          └──────────┬───────────┘
                                                     │ per row
                                                     ▼
                                          ┌──────────────────────┐
                                          │  reconcile /         │
                                          │  reflect / curate    │
                                          └──────────────────────┘
```

### 2.3 启动顺序

`webhook_server.py:main()`:

```python
1. configure_logging()
2. init_db()                            # CREATE TABLE IF NOT EXISTS memory_units
3. poller = ReconcilerPoller(...)
4. poller.start()                       # 立即进入空轮询,等 webhook 落库
5. try:
6.     server.serve_forever()
7. finally:
8.     poller.stop()                    # 等当前行处理完再退出
```

---

## 3. 数据库 schema

### 3.1 `memory_units` 表

```sql
CREATE TABLE IF NOT EXISTS memory_units (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    -- 业务键(bank_id + unit_id 唯一)
    bank_id             VARCHAR(255)    NOT NULL,
    unit_id             VARCHAR(64)     NOT NULL,

    -- 内容与时间(注意时间来自 Hindsight,不是本地时间)
    content             TEXT            NOT NULL,
    created_at          DATETIME        NOT NULL,
    document_id         VARCHAR(255)    DEFAULT NULL,

    -- 状态机
    status              ENUM('pending','processing','processed','superseded','failed')
                        NOT NULL DEFAULT 'pending',
    superseded_reason   TEXT            DEFAULT NULL,
    failure_reason      TEXT            DEFAULT NULL,

    -- 时间戳
    ingested_at         DATETIME        NOT NULL,
    processed_at        DATETIME        DEFAULT NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_bank_unit (bank_id, unit_id),
    KEY idx_status_created (status, created_at DESC),
    KEY idx_status_ingested (status, ingested_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**字段说明**:

- `bank_id` + `unit_id` 是**业务唯一键**:不同 bank 可能撞 unit_id,加 `bank_id` 入唯一键是必要的;
- `created_at` ← Hindsight 返回的 unit 时间(优先级 `mentioned_at` → `date` → `ingested_at` 兜底);
- `ingested_at` ← 本地写入时间,作为"60s fallback 窗"和"最近活跃"查询依据;
- `status='processing'` 不需要专门的 `processing_started_at` 字段——单 poller 串行模型下,行被取走即处理,无中间态;
- `idx_status_created` 支持 poller 的核心查询 `WHERE status='pending' ORDER BY created_at DESC LIMIT 1`;
- `idx_status_ingested` 支持 fallback 路径"查最近 60s 落库的 unit"。

### 3.2 状态机

```
                    ┌─────────────┐
                    │   pending   │  ←── 新 INSERT
                    └──────┬──────┘
                           │ poller 选中
                           ▼
                    ┌─────────────┐
                    │ processing  │
                    └──────┬──────┘
              ┌────────────┼────────────┐
              │            │            │
              ▼            ▼            ▼
       ┌──────────┐  ┌──────────┐  ┌──────────┐
       │processed │  │superseded│  │  failed  │
       └──────────┘  └──────────┘  └──────────┘
```

- `pending` → `processing`:poller 选中行,UPDATE 状态;
- `processing` → `processed`:reflect + curate 全部成功,标完成;
- `processing` → `superseded`:本行被另一个 unit 的 reflect 判定为被顶替(本地表内 supersede);
- `processing` → `failed`:reflect 抛异常(TimeoutError / URLError / HindsightAPIError),记录 `failure_reason`;
- 失败不重试(用户拍板,2026-08-01 决定);
- 进程崩溃发生在 `processing` 中:重启后行仍为 `processing`——**当前设计接受这一点**,因为单 poller 串行下,只有 `kill -9` / OOM 能造成,发生概率低;未来如要兜底,加一个 `processing_started_at` 列 + 启动时清理即可(留作未来任务,本次不实现)。

---

## 4. upsert 规则(webhook 落库时)

webhook 线程拿到 unit 列表后,对每个 unit 调 `db.upsert_unit(bank_id, unit_id, content, created_at, document_id)`。

```sql
INSERT INTO memory_units
    (bank_id, unit_id, content, created_at, document_id, status, ingested_at)
VALUES
    (?, ?, ?, ?, ?, 'pending', NOW())
ON DUPLICATE KEY UPDATE
    content      = IF(content = VALUES(content), content, VALUES(content)),
    created_at   = IF(content = VALUES(content), created_at, VALUES(created_at)),
    document_id  = IF(content = VALUES(content), document_id, VALUES(document_id)),
    status       = IF(content = VALUES(content), status, 'pending'),
    ingested_at  = IF(content = VALUES(content), ingested_at, NOW());
```

语义:
- `(bank_id, unit_id)` 已存在且 `content` 一致 → **不变**(全部 IF 走原值);
- `(bank_id, unit_id)` 已存在但 `content` 不一致 → 更新三列,`status='pending'`,`ingested_at=NOW()`;
- `(bank_id, unit_id)` 不存在 → INSERT 新行,`status='pending'`。

**关键点**:`content` 不变时连 `ingested_at` 都不更新——这样 fallback 路径的 60s 窗判断仍然准确(只对"内容真变化"的行才刷新 ingested_at)。

---

## 5. poller 一次循环

```python
def _process_one(self) -> bool:
    """返回 True 表示处理了一行,False 表示没有 pending。"""
    # 1. 候选行(SELECT ... FOR UPDATE 在单 poller 下无锁竞争,保留以便未来扩)
    row = db.fetchone("""
        SELECT id, bank_id, unit_id, content, created_at
        FROM memory_units
        WHERE status='pending'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        FOR UPDATE
    """)

    if row is None:
        return False

    # 2. 标记 processing
    db.execute("""
        UPDATE memory_units SET status='processing'
        WHERE id=%s
    """, (row.id,))

    # 3. reflect(structured_only=True 修复 #2)
    try:
        result = client.reflect(
            bank_id=row.bank_id,
            unit_id=row.unit_id,
            content=row.content,
            exclude_unit_ids=[row.unit_id],
        )
    except (HindsightAPIError, TimeoutError, URLError, OSError) as e:
        short = f"{type(e).__name__}: {str(e)[:200]}"
        db.execute("""
            UPDATE memory_units
            SET status='failed', failure_reason=%s, processed_at=NOW()
            WHERE id=%s
        """, (short, row.id))
        logger.exception("reflect failed for unit %s", row.unit_id)
        return True

    # 4. 提取 superseded IDs(只信 structured_output,不再回退扫 UUID —— #2 修复)
    superseded_ids = extract_superseded_ids(result, structured_only=True)
    reasoning_summary = (result.get("reasoning") or "")[:200]

    # 5. curate:Hindsight 端 PATCH state=invalidated(走现有 invalidate_memory)
    for sid in superseded_ids:
        try:
            client.invalidate_memory(sid, reason=reasoning_summary)
        except HindsightAPIError as e:
            logger.warning("invalidate %s failed: %s", sid, e)
            # 局部失败不重试,继续(项目惯例:curation 顶层结果保持 ok)

    # 6. 本地表:被 supersede 命中的本地行软标记
    if superseded_ids:
        placeholders = ",".join(["%s"] * len(superseded_ids))
        db.execute(f"""
            UPDATE memory_units
            SET status='superseded',
                superseded_reason=%s,
                processed_at=NOW()
            WHERE bank_id=%s
              AND unit_id IN ({placeholders})
              AND status IN ('pending','processing','processed')
        """, (reasoning_summary, row.bank_id, *superseded_ids))
        # 注:不会改到 row 自身,因为 row.status='processing' 不在 IN 里

    # 7. 本行标完成
    db.execute("""
        UPDATE memory_units
        SET status='processed', processed_at=NOW()
        WHERE id=%s
    """, (row.id,))

    return True
```

**循环**:
```python
def run(self):
    while not self._stop_event.is_set():
        try:
            processed = self._process_one()
        except Exception:
            logger.exception("poller loop error")
            processed = False
        if not processed:
            self._stop_event.wait(self._poll_interval_sec)  # 默认 1s
```

---

## 6. webhook 主路径 + fallback 路径

### 6.1 共用落库函数

两个路径都走同一个 `_ingest_units(bank_id, units)` 函数,只在前置"找 docId"那一步分叉。

```python
def _ingest_units(bank_id: str, units: list[dict]) -> dict:
    """落库并返回统计 {'inserted':N, 'updated':M, 'skipped':K}。"""
    stats = {"inserted": 0, "updated": 0, "skipped": 0}
    for u in units:
        unit_id = u.get("id")
        text    = u.get("text")
        if not isinstance(unit_id, str) or not isinstance(text, str):
            continue
        # 时间字段优先级:mentioned_at → date → 兜底 NOW()
        created_at = _parse_hindsight_time(
            u.get("mentioned_at") or u.get("date")
        )
        document_id = u.get("document_id") if isinstance(u.get("document_id"), str) else None
        outcome = db.upsert_unit(bank_id, unit_id, text, created_at, document_id)
        stats[outcome] += 1
    return stats
```

### 6.2 主路径

```python
def _process_post(body: dict) -> int:
    evt = parse_event(body)               # 已含 bank_id, document_id 可为 None
    if evt is None:
        return 200  # malformed 静默丢弃

    doc_id = evt.data.document_id
    if doc_id:
        units = client.list_memory_units(bank_id=evt.bank_id, document_id=doc_id, ...)
        _ingest_units(evt.bank_id, units)
    else:
        recovered = _try_recover_document_id(evt)   # fallback
        if recovered is None:
            logger.info("fallback rejected event %s", evt.operation_id)
            return 200
        doc_id = recovered
        units = client.list_memory_units(bank_id=evt.bank_id, document_id=doc_id, ...)
        _ingest_units(evt.bank_id, units)

    return 200
```

### 6.3 fallback 路径(已存在的逻辑复用)

`_try_recover_document_id(evt)` 沿用现有 `fetch_recent_doc` + `_within_fallback_window` 逻辑
(`webhook_handlers.py:241-259, 384-433`):

1. `units = client.list_recent_units(bank_id, limit=5)`
2. 从最近一条有 `document_id` 的 unit 读 `document_id` 和 `mentioned_at`
3. 60s 窗校验(`FALLBACK_TIMESTAMP_WINDOW_SECONDS=60`)
4. 通过 → 返回 docId;不通过 → 返回 None

**新方案下,fallback 找到 docId 之后,直接走主路径的 `list_memory_units + _ingest_units`**,
不再走"按 fallback 自己的方式处理这一批 units"——统一了代码路径。

### 6.4 fallback 是否要扫本地表?

不。**保留现有 `list_recent_units` 路径**,理由:
- 已有 60s 窗验证,2026-07-30 端到端测试已覆盖;
- 容器刚启动时本地表空,扫本地表会失败,要再回退到 Hindsight 端,**两级回退复杂度不值得**;
- 改动越小越好。

---

## 7. reflect 提取逻辑修复(#2)

`reflect_query.py` 的 `extract_superseded_ids(result, structured_only=True)` 行为变化:

- **默认 `structured_only=True`**:如果 `structured_output.superseded_fact_ids` 存在(可能为空列表)→ 只用它;
- 不再扫描 `reasoning` 文本中的 UUID;
- 显式传 `structured_only=False` 才走旧的 fallback(留给调试,生产不传)。

效果:对日志证据二那种"raw_ids=0,reasoning 里提到 UUID 但说没冲突"的情况,`kept_ids` 会是 0,
不再误清理。

---

## 8. reconcile.py 签名变更

旧:
```python
def run_reconcile(event: RetainEvent, *, bank_id: str, config_loader=...) -> ReconcileResult:
    # 整批 units 一起处理
    ...
```

新:
```python
def run_reconcile(bank_id: str, unit_id: str, content: str, *, config_loader=...) -> ReconcileResult:
    # 单条 unit 处理
    ...
```

内部 `exclude_unit_ids=[unit_id]` 仍保留(防自杀,见 commit `a4ac52d`)。

`include_based_on=False` 字段继续传(虽然被 Hindsight 忽略,#5),不主动去掉——避免静默改变请求体,
Hindsight 后续若恢复支持,无需同步改动。

---

## 9. 配置项

新增环境变量(在 `.env.example` 中加):

| 变量 | 默认 | 说明 |
|---|---|---|
| `HINDSIGHT_MYSQL_HOST` | `127.0.0.1` | MySQL 主机 |
| `HINDSIGHT_MYSQL_PORT` | `3306` | MySQL 端口 |
| `HINDSIGHT_MYSQL_USER` | `memorial` | 用户名 |
| `HINDSIGHT_MYSQL_PASSWORD` | (必填) | 密码(secret,不进 git) |
| `HINDSIGHT_MYSQL_DATABASE` | `hindsight_memorial` | 数据库名 |
| `HINDSIGHT_POLLER_INTERVAL_SEC` | `1` | poller 空轮询间隔 |
| `HINDSIGHT_POLLER_ENABLED` | `1` | `0` 可关闭 poller(用于本地调试) |

无 MySQL 配置时:**不启动 poller,只做落库**(等同把 memorial 退化成"收集器"——用于先部署观察一段时间)。

---

## 10. 依赖与部署

### 10.1 新运行时依赖

`pyproject.toml`:
```toml
dependencies = [
    "PyMySQL>=1.1.0",   # 纯 Python,Windows/Linux 通吃,免编译
]
```

### 10.2 docker-compose

新增 `mysql` service:
```yaml
services:
  mysql:
    image: mysql:8
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD:?must set}
      MYSQL_DATABASE: ${HINDSIGHT_MYSQL_DATABASE}
      MYSQL_USER: ${HINDSIGHT_MYSQL_USER}
      MYSQL_PASSWORD: ${HINDSIGHT_MYSQL_PASSWORD}
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 5s

  memorial:
    depends_on:
      mysql:
        condition: service_healthy
    environment:
      HINDSIGHT_MYSQL_HOST: mysql
      HINDSIGHT_MYSQL_PORT: 3306
      ...
```

外部已有 MySQL 的部署:.env 直接配 `HINDSIGHT_MYSQL_HOST=...`,不启 service。

### 10.3 启动时自动建表

`db.init_db()` 在 memorial 启动时跑 `CREATE TABLE IF NOT EXISTS`,幂等。
不做 migration 工具,字段调整靠 `ALTER TABLE`(未来如频繁变动,加 alembic)。

---

## 11. 弃用 / 改动清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `hindsight_memorial/dispatch.py` | **删除** | 整个文件退役 |
| `hindsight_memorial/webhook_server.py` | 改 | main() 增 init_db + poller 生命周期 |
| `hindsight_memorial/webhook_handlers.py` | 改 | 去掉 `_process_post` 里对 dispatcher 的依赖,改用 `_ingest_units` |
| `hindsight_memorial/reconcile.py` | 改 | 签名 `run_reconcile(bank_id, unit_id, content)` |
| `hindsight_memorial/reflect_query.py` | 改 | 默认 `structured_only=True` |
| `hindsight_memorial/curate.py` | 改 | 增 `curate_superseded_in_db(bank_id, ids, reason)` |
| `hindsight_memorial/db.py` | **新增** | DB 连接 + schema + upsert + 查询辅助 |
| `hindsight_memorial/poller.py` | **新增** | ReconcilerPoller |
| `hindsight_memorial/config.py` | 改 | 增 MySQL 配置加载 |
| `pyproject.toml` | 改 | 增 PyMySQL 依赖 |
| `docker-compose.yml` | 改 | 增 mysql service + memorial depends_on |
| `.env.example` | 改 | 增 MySQL 相关键 |
| `Dockerfile` | 不动 | 镜像构建无变化,只改运行时配置 |
| `tests/test_*.py` | 改/增 | 现有 dispatch 测试删除,新增 db/poller 测试 |

---

## 12. 风险与回滚

### 12.1 风险

- **PyMySQL 兼容性**:PyMySQL 在 Windows + Python 3.13 上验证过,但首次上生产前应在 staging 跑一遍
  webhook 流量回放(`scripts/replay_incident.py` 改用 MySQL 重写);
- **单 poller 性能**:理论瓶颈,1s 间隔下每分钟 60 行,195 units 也要 3 分钟+——比当前快,且
  worker 不再因单条超时卡死;
- **DB 连接管理**:PyMySQL 默认无连接池,poller 长连接 + 每次 webhook 短连接;
  需在 `db.py` 内做基础池化(简单的线程局部 connection)。

### 12.2 回滚

`HINDSIGHT_POLLER_ENABLED=0` 时:
- 启动时**不创建 poller 线程**;
- 落库仍发生(主路径不变);
- 不发生任何 reflect / curate;
- 行为等同"只采集不处理",可与回滚到旧代码同时部署观察。

---

## 13. 验证清单

按 verification-before-completion 原则,本次实现必须通过:

1. `python -m pytest tests/ -v` 全绿(改 + 增);
2. `python scripts/replay_incident.py` 改造后,跑通"重复 webhook 二次入队"场景,且
   第二次只处理 1 条 unit(增量的 3 条);
3. 手动构造 `data={}` webhook,确认 60s 窗仍按 docId 找到后**只入队一次**(dedup 跨 webhook);
4. 启动时 `mysql> SELECT status, COUNT(*) FROM memory_units GROUP BY status;` 能看到数据累计;
5. `docker compose down && docker compose up -d --build` 完整重启后,poller 自动继续处理遗留 `pending` 行。

---

## 14. 不在本次范围

- alembic 迁移工具(后续加);
- `processing` 状态的卡死回收(`processing_started_at` + 启动清理)——留 TODO;
- 多 poller 并发(`FOR UPDATE` 锁已留,未来扩展点);
- 跨进程 dedup 容器化(目前单实例,够用);
- 处理失败指标上报(目前 log 即可)。

---

## 15. 待用户确认的设计点

1. ~~supsersede 的本地行硬删还是软标记~~ — **已确认软标记 + superseded_reason 字段**;
2. ~~poller 线程放哪、怎么启停~~ — **已确认 webhook_server.py main 启停,ReconcilerPoller 类**;
3. ~~fallback 路径保留还是删~~ — **已确认保留,逻辑与主路径共用 `_ingest_units`**;
4. ~~MySQL 选型~~ — **已确认 MySQL(用户偏好,后续可迁 SQLite)**;
5. ~~失败重试~~ — **已确认不重试,记录 failure_reason,log 完整堆栈**;
6. **MySQL 来源** — **对接外部已有 MySQL**(用户 2026-08-01 决定);
7. **fallback 时是否复用主路径 `_ingest_units`** — **已确认复用**(本次设计第 6 节);
8. **时间字段取值优先级** — **本次设计 3.1 节默认 `mentioned_at` → `date` → `ingested_at` 兜底**,待你点头。

