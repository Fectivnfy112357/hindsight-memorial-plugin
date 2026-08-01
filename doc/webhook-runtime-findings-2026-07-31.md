# Webhook 运行时问题记录（2026-07-31）

本文记录 2026-07-31 对线上 `hindsight-memorial` 日志和 Hindsight 容器日志的只读核查结果。本文只记录已观察到的行为、证据和影响，不表示这些问题已经修复。

## 运行环境

- Memorial 日志：`/www/dk_project/dk_app/hindsight-memorial/data/logs/hindsight-memorial.log`
- Memorial 容器：`hindsight-memorial:local`
- Hindsight 容器：`ghcr.io/vectorize-io/hindsight:latest-slim`
- Memorial API 地址：`http://hindsight:8888`
- Memorial 默认 HTTP client timeout：180 秒（`hindsight_memorial/client.py:18, 68-72`）
- Memorial 使用单个后台 worker 串行处理 webhook 中的 memory units（`hindsight_memorial/dispatch.py`）

## 1. 一个 `document_id` 可能触发累计文档中全部 memory units 的重复 reflect

### 已确认的处理流程

Hindsight 的 webhook 不携带全部记忆内容，只携带 `document_id`。Memorial 收到 webhook 后执行：

```text
retain.completed webhook（包含 document_id）
    ↓
GET /memories/list?document_id=<document_id>
    ↓
返回该 document 当前累计的全部 memory units
    ↓
逐个 unit 调用 Hindsight reflect
```

对应代码：

- `hindsight_memorial/client.py:213-251`：按 `document_id` 分页查询 units；
- `hindsight_memorial/webhook_handlers.py:476-520`：遍历查询结果；
- `hindsight_memorial/reconcile.py:175-192`：每个 unit 单独调用 reflect。

### 日志证据

同一个 `document_id` `20260731_154948_6c281c` 出现了两次 webhook：

第一次：

```text
08:15:54 UTC
fetched units ... document=20260731_154948_6c281c units=3
```

第二次：

```text
08:27:09 UTC
fetched units ... document=20260731_154948_6c281c units=6
```

Hindsight 容器日志解释了第二次查询结果变大的原因：

```text
16:27:09 +0800
[append] Prepended 5,726 chars from existing document 20260731_154948_6c281c
Document: 20260731_154948_6c281c
```

也就是说，第二次 retain 是对已有文档做 append，而不是创建新的 document。该文档从 3 个累计 unit 增长到 6 个 unit。Memorial 第二次仍然按同一个 `document_id` 查询整个文档，因此第一次已经处理过的 unit 又被取回并再次 reflect。

### 影响

- 同一个 unit 可能跨多个 webhook 重复 reflect；
- 由于每次 reflect 都可能返回不同结果，重复处理会增加 LLM 配额消耗和误清理机会；
- 当前 webhook body 去重不能解决这个问题，因为两次 webhook body 不同；
- 当前代码没有按 `unit_id` 做跨 webhook 的持久去重，也没有记录某个 document 已处理到哪个增量位置。

### 证据边界

这次日志能够确认的是“两次 webhook 指向同一个可 append 的 document，导致部分 unit 被重复处理”。不能据此断言 Hindsight 在单次 list 响应内部返回了重复记录。

## 2. structured verdict 为空时，reasoning 中的 UUID 仍可能被当成待清理 ID

### 已确认的行为

`hindsight_memorial/reflect_query.py:67-118` 的提取逻辑是：

1. 优先读取 `structured_output.superseded_fact_ids`；
2. 如果该列表为空，则继续扫描 `reasoning` 和普通文本中的 UUID；
3. 将扫描到的 UUID 当作 `superseded_fact_ids` 返回。

对于破坏性操作，这会把“解释文本中提到的 UUID”误认为“需要 invalidate 的 UUID”。

### 日志证据一

```text
05:48:15 UTC
reflect verdict: bank=hindsight-memorial raw_ids=0 kept_ids=2 ids=[...]
reasoning="... no UUIDs match the criteria ... no facts have their truth value materially negated ..."

unit 1/195 result=ok superseded=2 observations_cleared=2
```

这里 `raw_ids=0`，reasoning 明确表示没有匹配项，但 fallback 仍然抽取了 2 个 UUID 并执行了清理。

### 日志证据二

```text
08:30:07 UTC
reflect verdict: bank=hermes-agent raw_ids=0 kept_ids=1 ids=['eab54af1-...']
reasoning='No facts were found ... existing observation with ID eab54af1-... contains identical wording ...'

unit 4/6 result=ok superseded=1 observations_cleared=0
```

这里 reasoning 说明已有 observation 与新事实相同，并没有说要淘汰该 ID，但该 UUID 仍进入了清理列表。

### 影响

这会造成错误的 memory invalidation 和 observation 清理，是当前已从线上日志确认的记忆污染风险。对于明确存在但为空的结构化数组，应该把空数组理解为“没有待清理 ID”，而不是退回自然语言 UUID 扫描。

## 3. 单个 reflect 超时会中断整个 document 批次，剩余 units 不会续跑

### 日志证据

```text
05:47:14 UTC
fetched units ... document=494cf752-7661-4dcc-a4a8-b91b6193d4fd units=195
```

随后 memorial 按顺序处理 unit。处理到前几个 unit 后：

```text
05:56:49 UTC
ERROR hindsight_memorial.dispatch processing failed: key=879af7c3a88f elapsed=575.7s
...
TimeoutError: timed out
```

异常堆栈落在：

```text
hindsight_memorial/reconcile.py:177  client.reflect(...)
hindsight_memorial/client.py:97    urllib.request.urlopen(... timeout=self.timeout)
```

Hindsight 容器日志显示对应的 reflect 请求最终耗时约 191 秒：

```text
13:56:25 +0800
APIConnectionError ... Request timed out

13:57:01 +0800
reflect ... total=191.584s
```

Memorial 的 180 秒 client timeout 先于 Hindsight 返回触发了 `TimeoutError`。

### 当前行为

- 异常从单个 `run_reconcile()` 冒出；
- `handle_event()` 的 unit 循环被中断；
- 同一个 document 中尚未处理的 unit 不会继续处理；
- Dispatcher 记录 `processing failed`，不会从中断位置恢复；
- 该 webhook 后续不会自动续跑剩余 units。

此外，`client.py:97-108` 当前显式包装的是 `HTTPError` 和 `URLError`，这次实际出现的原始 `TimeoutError` 没有被转换成统一的 `HindsightAPIError`，因此没有进入正常的 `reflect_failed` 结果路径。

## 4. 大 document 会造成极长的串行处理时间

这不是 webhook 一次传递了大量记忆，而是一个 webhook 的 `document_id` 查询返回了大量累计 units，随后 memorial 逐个反思。

本次日志中的例子：

```text
195 units → 在第 7 个左右处理阶段遇到超时，整个批次失败
8 units   → 362.0s
3 units   → 158.0s
6 units   → 265.8s
```

`list_memory_units()` 在 `hindsight_memorial/client.py:237-250` 只设置了最多 50 页、每页 100 条的保护，即最多查询 5000 条；这不是合理的业务批次限制。因为 worker 是单线程串行的，大 document 会长时间占用 worker，使其他 webhook 排队。

这里记录的是吞吐和故障隔离问题，不表示要立即通过并发处理解决；并发 reflect 可能改变同一 bank 的顺序、去重和限流语义。

## 5. `include_based_on` 与当前 Hindsight API 存在接口漂移

Memorial 在 `hindsight_memorial/reconcile.py:175-181` 调用 reflect 时传入：

```python
include_based_on=False
```

`hindsight_memorial/client.py:125-132` 会把它写入请求体。但 Hindsight 容器日志多次出现：

```text
WARNING hindsight_api.api.http
Unknown parameters ignored: [include_based_on]
for POST /v1/default/banks/hindsight-memorial/reflect
```

因此当前 Hindsight 版本会忽略这个字段。已确认的事实是参数被忽略；仅凭此日志不能断言它已经直接造成错误清理，但它说明 memorial 与 Hindsight API 的 reflect 请求契约已经发生漂移，需要单独核对当前 API schema。

## 不纳入本次问题清单的刻意设计

以下行为已确认是当前设计，不作为本次运行时问题记录：

- webhook admission 无论签名错误或处理失败都尽量返回 HTTP 200，以避免触发 Hindsight webhook retry ladder；
- webhook 返回给 Hindsight 的结果不是完整 reconcile 成功报告，详细结果写入 memorial 日志；
- curation 顶层结果保持 `ok` 的现有语义，单个 PATCH/observation 清理结果另行记录；
- `memory_unit_count=0` 是 webhook 中的提示值，memorial 仍按 `document_id` 查询实际 units；本次不把它作为独立问题处理。

## 相关线上日志片段索引

- 超时异常：`hindsight-memorial.log:72-138`
- 195 units 批次：`hindsight-memorial.log:31-72`
- 同 document 两次查询：`hindsight-memorial.log:454-471`、`494-524`
- `raw_ids=0` 仍清理 UUID：`hindsight-memorial.log:37`、`513`
- `include_based_on` 被忽略：Hindsight 容器日志对应各次 reflect 请求的 `Unknown parameters ignored` 警告。
