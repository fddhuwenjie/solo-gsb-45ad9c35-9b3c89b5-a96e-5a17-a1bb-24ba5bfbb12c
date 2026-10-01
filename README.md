# 分时阶梯计价 API（FastAPI + SQLite）

按**版本化费率规则**对带时区的用量区间做分时（TOU）+ 累计阶梯计价。
所有时间窗口均为**半开区间 `[start, end)`**，金额与用量全程使用 `Decimal`，
内置跨日、夏令时（DST）切换、工作日/节假日（含调休）与多段阶梯的精确切分。

## 一条命令启动

```bash
docker compose up --build
```

服务在 **http://localhost:8000** 提供（Docker Compose 启动、端口 **8000**
保持不变）：

- Swagger UI：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health

SQLite 数据文件持久化在 Docker 卷 `tariff-data`（容器内 `/data/tariff.db`），
首次启动自动建表并写入预置规则版本。在线备份发布到同一卷的
`/data/backups/`（见 [`POST /backups/{backup_id}`](#post-backupsbackup_id)）。

> 不用 Docker 也可以：
> `pip install -r requirements.txt && uvicorn app.main:app --port 8000`
> （默认使用本地 `./data/tariff.db`，可用环境变量 `DATABASE_URL` 覆盖）

## 预置规则版本

| 版本号 | 时区 | 说明 |
| --- | --- | --- |
| `us-ny-demo-v2024` | `America/New_York` | 2024 年纽约演示费率：夏令时/标准时工作日 + 周末节假日；含 2024 全年美国公共假日；三段累计阶梯（100 / 300 / ∞ kWh） |
| `us-ny-demo-v2025` | `America/New_York` | 2024-07-01 起生效的新版纽约演示费率：结构与 v2024 相同、单价上调，节假日日历略有差异（2024-10-14 不再列为节假日）；用于 `/quote-diff` 版本对比演示 |
| `cn-shanghai-demo-v2024` | `Asia/Shanghai` | 2024 年中国居民阶梯分时演示：工作日（含**调休上班**）/ 普通周末 / 法定节假日；三段累计阶梯（200 / 400 / ∞ kWh） |

> 价格均为演示值，不代表真实电价。

### 规则模型（SQLite 表）

- `tariff_versions`：版本号、币种、能量单位、描述。
- `rate_rules`：规则代码、**生效时间段（规则时区的本地朴素时间）**、**时区**、
  ISO 星期掩码（`1..7`，逗号分隔，空=每天）、`day_scope`
  （`all` / `workdays` / `holidays` / `non_workdays` / `weekends`）、
  日内时段 `time_on` / `time_off`（半开，支持 `24:00` 日末和跨午夜时段）。
- `tiers`：阶梯序号、**累计用量上限**（最后一档必须为空，表示开放）与单价。
- `holidays`：版本绑定的日历，`kind=holiday` 为法定假日，`kind=workday`
  为调休上班日（如中国周末调休）。
- `version_imports`：`POST /versions/import` 的导入登记（版本号、内容摘要、
  首次响应体），用于幂等重放与同 code 冲突检测。
- `billing_cycles` / `usage_batches` / `cycle_intervals`：
  `POST /cycles/{id}/batches` 的账期累计状态（锁定的规则版本、当前
  revision、未舍入累计金额）、每批幂等登记（幂等键、内容摘要、首次响应
  体、本批金额）与已合并的 UTC 用量区间。
- `database_backups`：`POST /backups/{id}` 的备份登记（backup_id、
  文件名、字节数、SHA-256、发布时间），用于幂等重放、不可覆盖判定与
  重启后的下载/核对；备份文件本身位于数据卷的 `backups/` 目录。

规则在 `app/database.py` 的 `SEED_VERSIONS` 中声明，改完后清空数据库卷重启即可重新播种：

```bash
docker compose down -v && docker compose up --build
```

## 接口

### `POST /quote`

请求：

```json
{
  "version": "us-ny-demo-v2024",
  "intervals": [
    {
      "start": "2024-03-09T23:30:00-05:00",
      "end": "2024-03-10T05:30:00-04:00",
      "timezone": "America/New_York",
      "energy": 10
    }
  ]
}
```

- `intervals` 支持多段，按时间排序后统一计价；段与段不得重叠。
- 建议时间携带明确 UTC 偏移量（`-05:00` / `Z`）。若给**朴素时间**，
  则按 `timezone` 解释；落在夏令时**春季缺口**或**秋季歧义**的朴素时间会被拒绝（400），
  要求携带偏移量消歧。
- 累计阶梯以**整个报价的累计用量**为准（跨规则共享），跨档时按时间比例切出
  半开子段，舍入残差并入末段，保证分段能量之和严格等于输入能量。

响应（节选）：

```json
{
  "version": "us-ny-demo-v2024",
  "currency": "USD",
  "energy_unit": "kWh",
  "total_energy": "10.000000",
  "total_amount": "1.50",
  "matched_rules": ["leisure"],
  "segments": [
    {
      "interval_index": 0,
      "start": "2024-03-10T04:30:00Z",
      "end": "2024-03-10T05:00:00Z",
      "start_local": "2024-03-09T23:30:00-05:00",
      "end_local": "2024-03-10T00:00:00-05:00",
      "timezone": "America/New_York",
      "duration_seconds": 1800,
      "day_kind": "weekend",
      "energy": "1.000000",
      "rule_code": "leisure",
      "rule_name": "周末与节假日（全天）",
      "tier_ordinal": 1,
      "cumulative_from": "0.000000",
      "cumulative_to": "1.000000",
      "unit_price": "0.15",
      "amount": "0.150000"
    }
  ]
}
```

逐段字段含义：UTC 与本地两种表示的半开窗口、UTC 秒级时长、当日类型
（`weekday`/`weekend`/`holiday`/`workday`）、分摊能量、命中规则、所在阶梯、
该段在累计阶梯中的用量区间、单价与金额。`total_amount` 四舍五入到分。

### 夏令时示例

2024 年纽约春令时在 **3 月 10 日 02:00 → 03:00** 跳时。上面的请求从
3/9 23:30 到 3/10 05:30（本地），钟表走了 6 小时，但实际只覆盖 **5 个 UTC 小时**，
切分点为本地午夜 `00:00` 与跳时刻（不存在的 `02:00`）。

秋令时回拨（11 月 3 日 02:00 → 01:00，重复一小时）需带偏移量，例如
`2024-11-03T00:00:00-04:00` 到 `2024-11-03T08:00:00-05:00` 覆盖 9 个 UTC 小时。

### 跨规则 + 跨阶梯示例

美国独立日（2024-07-04）前夜到当天凌晨：

```bash
curl -s -X POST localhost:8000/quote -H 'Content-Type: application/json' -d '{
  "version": "us-ny-demo-v2024",
  "intervals": [{
    "start": "2024-07-03T22:00:00-04:00",
    "end":   "2024-07-04T06:00:00-04:00",
    "timezone": "America/New_York",
    "energy": 150
  }]
}'
```

结果在本地午夜切为工作日段（`dst-workday`，2h，37.5 kWh @0.22）与
节假日段（`leisure`，6h，112.5 kWh）；节假日段又在累计用量 100 kWh 处
切成两档（62.5 @0.15、50 @0.18），总价 **26.63 USD**。

### `POST /quote-diff`

对**同一组用量区间**分别用两个版本的计价引擎独立报价，再逐段对比。
两版**币种或能量单位不同**直接拒绝（422 `incompatible_versions`）。

切分方式：取两版计费段的 **UTC 端点并集**切成原子段；每个原段的能量与
六位小数金额按**实际时长**比例摊到它的原子子段，舍入余量归入该原段的
**最后一个子段**——因此每版原子段的逐段之和与汇总都和 `POST /quote`
的独立报价**严格一致**。未发生变化的原子段也会保留（`reasons` 为空）。

请求（`intervals` 与 `/quote` 完全同格式；区间解析、结构校验与报错语义
也和 `/quote` 一致）：

```bash
curl -s -X POST localhost:8000/quote-diff -H 'Content-Type: application/json' -d '{
  "before_version": "us-ny-demo-v2024",
  "after_version":  "us-ny-demo-v2025",
  "intervals": [{
    "start": "2024-07-03T22:00:00-04:00",
    "end":   "2024-07-04T06:00:00-04:00",
    "timezone": "America/New_York",
    "energy": 150
  }]
}'
```

响应（节选）：

```json
{
  "before_version": "us-ny-demo-v2024",
  "after_version": "us-ny-demo-v2025",
  "currency": "USD",
  "energy_unit": "kWh",
  "total_energy": "150.000000",
  "before_total_amount": "26.63",
  "after_total_amount": "28.13",
  "total_amount_diff": "1.50",
  "before_matched_rules": ["dst-workday", "leisure"],
  "after_matched_rules": ["dst-workday", "leisure"],
  "segments": [
    {
      "interval_index": 0,
      "start": "2024-07-04T02:00:00Z",
      "end": "2024-07-04T04:00:00Z",
      "duration_seconds": 7200,
      "before": {"rule_code": "dst-workday", "rule_name": "夏令时 工作日",
                 "tier_ordinal": 1, "unit_price": "0.22",
                 "energy": "37.500000", "amount": "8.250000"},
      "after":  {"rule_code": "dst-workday", "rule_name": "夏令时 工作日",
                 "tier_ordinal": 1, "unit_price": "0.23",
                 "energy": "37.500000", "amount": "8.625000"},
      "energy_diff": "0.000000",
      "amount_diff": "0.375000",
      "reasons": ["unit_price_changed"]
    }
  ]
}
```

每个原子段给出两版的规则、阶梯、单价、能量与金额，以及差额
（`amount_diff` / `energy_diff` = after − before）。`reasons` 列出
**同时成立**的变化维度：

| reason | 含义 |
| --- | --- |
| `rule_changed` | 两版命中的规则代码不同 |
| `tier_changed` | 两版所在阶梯序号不同 |
| `unit_price_changed` | 两版单价不同 |

两版节假日日历不同也会导致 `rule_changed`。例如 2024-10-14（哥伦布日）
在 v2024 中是节假日、在 v2025 中是普通工作日：

```bash
curl -s -X POST localhost:8000/quote-diff -H 'Content-Type: application/json' -d '{
  "before_version": "us-ny-demo-v2024",
  "after_version":  "us-ny-demo-v2025",
  "intervals": [{
    "start": "2024-10-14T09:00:00-04:00",
    "end":   "2024-10-14T17:00:00-04:00",
    "timezone": "America/New_York",
    "energy": 80
  }]
}'
```

该窗口内每个原子段都是 `leisure`（@0.15）对 `dst-workday`（@0.23），
`reasons` 为 `["rule_changed", "unit_price_changed"]`，总价 12.00 →
18.40，差额 **6.40 USD**。

### `POST /shift-plan`

为**两项可移时用电任务**在各自可用窗口内寻找**总费用最低**的开机时刻组合。

请求给出规则版本、IANA 时区，以及恰好两项任务：

- `earliest_start` / `latest_end`：最早开机与最晚完工的 ISO-8601 时刻，
  **必须携带 UTC 偏移量**（如 `-04:00` / `Z`），任务须在 `latest_end`
  **之前**结束（半开区间）；两者跨度（可用窗口）**最多 8 小时**；
- `duration_minutes`：整数分钟的连续运行时长（必须能在窗口内放下）；
- `energy`：正用量（如 kWh）。

枚举与选优规则：

- 以**每项任务最早开机的 UTC 时刻为起点**，每隔 **30 分钟**枚举一个候选开机时刻
  （每项最多 17 个候选、组合数 ≤ 289）；
- 每组组合要求任务在最晚完工前结束、两任务**互不重叠**（半开区间，首尾相接允许）；
- 候选区间按开机时间排序后在**同一次** `/quote` 计价中统一报价，
  **累计阶梯跨两项任务共享**，绝不是分别报价再相加；
- 某组候选遇到**规则缺口**（`rule_gap`，某些时刻没有规则覆盖）只淘汰该组；
  版本存在**结构错误**（规则重叠、阶梯缺口）则直接返回 **409 `rule_conflict`**；
- 以**未舍入的六位小数金额**选最低费用；同价时取**输入顺序下 UTC 开机时间
  字典序最早**的组合（先比任务 0，再比任务 1）。

请求示例（服务端口 **8000**）：

```bash
curl -s -X POST localhost:8000/shift-plan -H 'Content-Type: application/json' -d '{
  "version": "us-ny-demo-v2024",
  "timezone": "America/New_York",
  "tasks": [
    {
      "earliest_start": "2024-07-05T20:00:00-04:00",
      "latest_end":     "2024-07-06T04:00:00-04:00",
      "duration_minutes": 120,
      "energy": 5
    },
    {
      "earliest_start": "2024-07-05T20:00:00-04:00",
      "latest_end":     "2024-07-06T04:00:00-04:00",
      "duration_minutes": 120,
      "energy": 7
    }
  ]
}'
```

上面两项任务的窗口横跨周五（`dst-workday` @0.22）与周六（`leisure` @0.15），
引擎会把两任务都排进周六凌晨。响应（`segments` 有省略，实际字段与 `/quote` 一致）：

```json
{
  "version": "us-ny-demo-v2024",
  "currency": "USD",
  "energy_unit": "kWh",
  "timezone": "America/New_York",
  "feasible": true,
  "total_energy": "12",
  "total_amount": "1.80",
  "tasks": [
    {"task_index": 0,
     "start": "2024-07-06T04:00:00Z", "end": "2024-07-06T06:00:00Z",
     "start_local": "2024-07-06T00:00:00-04:00",
     "end_local":   "2024-07-06T02:00:00-04:00",
     "timezone": "America/New_York",
     "duration_minutes": 120, "energy": "5.000000"},
    {"task_index": 1,
     "start": "2024-07-06T06:00:00Z", "end": "2024-07-06T08:00:00Z",
     "start_local": "2024-07-06T02:00:00-04:00",
     "end_local":   "2024-07-06T04:00:00-04:00",
     "timezone": "America/New_York",
     "duration_minutes": 120, "energy": "7.000000"}
  ],
  "segments": [
    {"task_index": 0, "start": "2024-07-06T04:00:00Z",
     "end": "2024-07-06T06:00:00Z", "rule_code": "leisure",
     "tier_ordinal": 1, "energy": "5.000000", "amount": "0.750000",
     "...": "其余字段与 /quote 的逐段输出相同（含本地时间、阶梯区间等）"},
    {"task_index": 1, "start": "2024-07-06T06:00:00Z",
     "end": "2024-07-06T08:00:00Z", "rule_code": "leisure",
     "tier_ordinal": 1, "energy": "7.000000", "amount": "1.050000",
     "...": "..."}
  ],
  "candidates_evaluated": 169,
  "non_overlapping_pairs": 90,
  "rule_gap_pairs": 0,
  "reason_code": null,
  "reason": null,
  "gaps": null
}
```

- `tasks`：各任务最终的 UTC / 本地起止时间与时长、用量；
- `segments`：与 `/quote` 相同的计价段，用 **`task_index`（0/1，即输入顺序）**
  标明每段归属哪项任务；`total_amount` 为四舍五入到分的合计；
- `candidates_evaluated` / `non_overlapping_pairs` / `rule_gap_pairs`：
  枚举总数、互不重叠的组合数、被规则缺口淘汰的组合数。

**无可行组合**时 HTTP 仍为 200，但 `feasible=false`，并给出机器可读原因
（`reason_code` + 中文说明 `reason`，规则缺口场景还会在 `gaps` 里给出
最多 5 组被淘汰候选及缺口时刻）：

| reason_code | 含义 |
| --- | --- |
| `no_candidate_start` | 某任务在窗口内没有任何满足 30 分钟网格、且能按时完工的开机时刻 |
| `no_non_overlapping_pair` | 两任务的所有候选组合都互相重叠 |
| `all_pairs_rule_gap` | 所有互不重叠的组合都在计价区间内遇到规则缺口 |

### `POST /meter-readings/reconcile`

把**累计表计读数**整理成可直接提交给 `/quote` 的 UTC 半开用量区间
（不触碰数据库，纯计算）。计数器按 `modulus` 回卷；跨 reset 或采样间隔
超限的范围只记为 gap，**不估算用量**。

请求：

```json
{
  "modulus": "10000",
  "max_interval_seconds": 3600,
  "resets": ["2024-07-06T08:00:00Z"],
  "readings": [
    {"at": "2024-07-06T00:00:00Z", "value": "9950"},
    {"at": "2024-07-06T01:00:00Z", "value": "20"},
    {"at": "2024-07-06T02:00:00Z", "value": "55"},
    {"at": "2024-07-06T05:00:00Z", "value": "80"},
    {"at": "2024-07-06T08:00:00Z", "value": "5"},
    {"at": "2024-07-06T09:00:00Z", "value": "45"}
  ]
}
```

- `readings`：**2～500 条**累计读数，`at` 必须携带 UTC 偏移量
  （`Z` / `±HH:MM`），`value` 必须落在 **`[0, modulus)`** 内；顺序任意。
- `modulus`：正数计数器模数（读数达到后回卷为 0）。
- `max_interval_seconds`：正整数秒的最大采样间隔。
- `resets`：可选的 reset 时刻（同样必须带偏移量），**每个都必须命中
  某条读数**，否则 400 `reset_not_on_reading`。

处理规则：

1. 全部读数先按 **UTC 时刻**排序；同一时刻**同值去重**（保留输入下标
   最靠前的一条，计入 `duplicate_count`）、**异值报错**
   （400 `conflicting_readings`）。
2. 命中 **reset** 的读数是一段新序列的起点：通向它的相邻读数对只记为
   gap（`reason="reset"`）。
3. 相邻读数间隔**超过** `max_interval_seconds` 的对同样只记为 gap
   （`reason="max_interval_exceeded"`）；恰好等于不算超限。
4. 其余相邻读数对生成半开区间 `[start, end)`：差值为正直接取用；
   **出现下降按一次 modulus 回卷**计算（`delta = cur − prev + modulus`）
   并置 `wrapped=true`；差值为 0 的静止对不产生区间（`/quote` 不接受
   0 能量），也不算 gap。

响应（上面请求的节选）：

```json
{
  "modulus": "10000",
  "reading_count": 6,
  "duplicate_count": 0,
  "total_energy": "145",
  "intervals": [
    {"start": "2024-07-06T00:00:00Z", "end": "2024-07-06T01:00:00Z",
     "duration_seconds": 3600, "from_index": 0, "to_index": 1,
     "wrapped": true, "energy": "70"},
    {"start": "2024-07-06T01:00:00Z", "end": "2024-07-06T02:00:00Z",
     "duration_seconds": 3600, "from_index": 1, "to_index": 2,
     "wrapped": false, "energy": "35"},
    {"start": "2024-07-06T08:00:00Z", "end": "2024-07-06T09:00:00Z",
     "duration_seconds": 3600, "from_index": 4, "to_index": 5,
     "wrapped": false, "energy": "40"}
  ],
  "gaps": [
    {"start": "2024-07-06T02:00:00Z", "end": "2024-07-06T05:00:00Z",
     "duration_seconds": 10800, "from_index": 2, "to_index": 3,
     "reason": "max_interval_exceeded"},
    {"start": "2024-07-06T05:00:00Z", "end": "2024-07-06T08:00:00Z",
     "duration_seconds": 10800, "from_index": 3, "to_index": 4,
     "reason": "reset"}
  ]
}
```

- `from_index` / `to_index` 是区间端点读数在**请求 `readings` 中的原始
  下标**（0 起，排序/去重不改变下标含义）。
- 用量为**精确值（未舍入）**。**对账不变量**：任一连续段（不被
  reset / 采样间隔打断）内区间用量之和 == 该段末读数 − 首读数 +
  modulus × 回卷次数；因此 `total_energy` 严格等于各区间用量之和
  （上例：回卷段 (20−9950+10000)+35 = 105，reset 后 45−5 = 40，
  合计 145）。

`intervals` 的 `start` / `end` / `energy` 与 `/quote` 的区间格式一致
（时间已带 `Z` 偏移，`timezone` 字段可任填，如 `UTC`），可以管道直
接接力报价：

```bash
curl -s -X POST localhost:8000/meter-readings/reconcile \
  -H 'Content-Type: application/json' -d @readings.json \
  | jq '{version: "us-ny-demo-v2024",
         intervals: [.intervals[] | {start: .start, end: .end,
                                     timezone: "UTC", energy: .energy}]}' \
  | curl -s -X POST localhost:8000/quote \
      -H 'Content-Type: application/json' -d @-
```

上面的演示负载经 `/quote` 报价得 `total_energy=145`、
`total_amount=30.55`（周五 `dst-workday` 与周六 `leisure` 分段计价，
累计阶梯跨区间共享）。

### `POST /demand-charge`

根据**不等长用量区间**结算**最大需量（maximum demand）费用**（纯计算，
不触碰数据库）。给出对齐用的 IANA 时区、需量窗时长（**15 / 30 / 60 分钟**）
与正单价（每 kW），服务端按**本地午夜起算的固定分钟边界**枚举窗口，
取**平均功率最高**的窗口按六位小数需量计费。`intervals` 与 `/quote`
完全同格式（可不等长、互不重叠、首尾相接允许）。

请求示例（服务端口 **8000**）：

```bash
curl -s -X POST localhost:8000/demand-charge -H 'Content-Type: application/json' -d '{
  "timezone": "America/New_York",
  "window_minutes": 15,
  "unit_price": "12.50",
  "intervals": [
    {"start": "2024-07-03T16:07:00-04:00", "end": "2024-07-03T16:22:30-04:00",
     "timezone": "America/New_York", "energy": "3.1"},
    {"start": "2024-07-03T16:22:30-04:00", "end": "2024-07-03T16:45:00-04:00",
     "timezone": "America/New_York", "energy": "4.5"},
    {"start": "2024-07-03T18:00:00-04:00", "end": "2024-07-03T18:30:00-04:00",
     "timezone": "America/New_York", "energy": "1"}
  ]
}'
```

窗口与夏令时语义：

- 窗口**按每个本地日重新对齐**：以当日本地午夜 `00:00` 的 UTC 时刻为起点，
  每隔 `window_minutes` 分钟沿 **UTC 时间线递增**取边界，直到次日本地午夜；
  普通日恰有 `1440/N` 个窗口（60 分钟档 24 个、15 分钟档 96 个）。
- **春季跳时**（如纽约 2024-03-10，02:00→03:00）整日为 23 个 UTC 小时：
  15 分钟档得到 92 个等长窗口，缺时区间内**不制造**不存在的本地边界。
- **秋季回拨**（如纽约 2024-11-03，02:00→01:00）整日为 25 个 UTC 小时：
  15 分钟档得到 100 个窗口；重复的本地边界（如两个本地 `01:15`，偏移量分别为
  `-04:00` 与 `-05:00`）对应不同的 UTC 窗口，**分别枚举、分别参评**
  （响应中用带偏移量的 `start_local` 区分）。
- 落在 DST 春季缺口或秋季歧义的**朴素时间**与 `/quote` 一样被拒绝
  （400），请携带明确 UTC 偏移量。

参评与分摊：

- 只有被输入区间（**首尾相接视为连续覆盖**）**完整覆盖**的窗口才参评；
  仅部分与输入相交的边缘窗口记 `partial_coverage`，完全落在内部缺口内的
  窗口记 `not_covered`（理论上的非整除碎片记 `incomplete_alignment`）。
- 跨窗区间按窗口与输入的**实际 UTC 微秒时长**精确比例分摊能量，每段输入
  独立在六位小数上分摊、舍入残差并入该输入的末切片——因此**逐输入严格
  能量守恒**（窗口各切片能量之和恰为该输入的申报能量），DST 日自然按
  23/25 小时计价。
- 以**未舍入能量**选出平均功率最高的窗口（参评窗口等长，功率与能量同序），
  **同值时取 UTC 起点最早者**；需量 kW = 窗口能量 ÷ 窗口小时数，
  需量与费用都按**六位小数四舍五入**计费（`demand_kw`、`amount`）。
- 费用再按获选窗口内各输入的能量占比分摊（`selected_window.allocations`），
  残差并入最后一项，各分摊费用之和严格等于总费用。该视图**只覆盖获选
  窗口**：跨窗输入落在其他窗口中的部分不出现在其中。
- 独立的 `interval_shares` 字段按 `interval_index` 分组返回**每条原始输入
  在全部重叠窗口中的切片**（含未参评窗口内的切片，按 UTC 时间递增），
  与获选窗口无关；每组的 `energy` 为各切片之和，**严格等于该输入的申报
  能量**——跨窗输入在其他窗口中的能量都能在此对账。

响应（节选）：

```json
{
  "timezone": "America/New_York",
  "window_minutes": 15,
  "unit_price": "12.50",
  "window_count": 10,
  "eligible_window_count": 4,
  "selected_window": {
    "window_index": 1,
    "start": "2024-07-03T20:15:00Z",
    "end": "2024-07-03T20:30:00Z",
    "start_local": "2024-07-03T16:15:00-04:00",
    "end_local": "2024-07-03T16:30:00-04:00",
    "duration_seconds": 900,
    "energy": "3.000000",
    "average_power_kw": "12.000000",
    "allocations": [
      {"interval_index": 0,
       "start": "2024-07-03T20:15:00Z", "end": "2024-07-03T20:22:30Z",
       "start_local": "2024-07-03T16:15:00-04:00",
       "end_local": "2024-07-03T16:22:30-04:00",
       "duration_seconds": 450,
       "energy": "1.500000", "demand_kw": "6.000000", "amount": "75.000000"},
      {"interval_index": 1,
       "start": "2024-07-03T20:22:30Z", "end": "2024-07-03T20:30:00Z",
       "start_local": "2024-07-03T16:22:30-04:00",
       "end_local": "2024-07-03T16:30:00-04:00",
       "duration_seconds": 450,
       "energy": "1.500000", "demand_kw": "6.000000", "amount": "75.000000"}
    ]
  },
  "demand_kw": "12.000000",
  "amount": "150.000000",
  "interval_shares": [
    {"interval_index": 0, "energy": "3.100000", "slices": [
      {"window_index": 0,
       "start": "2024-07-03T20:07:00Z", "end": "2024-07-03T20:15:00Z",
       "start_local": "2024-07-03T16:07:00-04:00",
       "end_local": "2024-07-03T16:15:00-04:00",
       "duration_seconds": 480, "energy": "1.600000"},
      {"window_index": 1,
       "start": "2024-07-03T20:15:00Z", "end": "2024-07-03T20:22:30Z",
       "start_local": "2024-07-03T16:15:00-04:00",
       "end_local": "2024-07-03T16:22:30-04:00",
       "duration_seconds": 450, "energy": "1.500000"}
    ]},
    {"interval_index": 1, "energy": "4.500000", "slices": [
      {"window_index": 1,
       "start": "2024-07-03T20:22:30Z", "end": "2024-07-03T20:30:00Z",
       "start_local": "2024-07-03T16:22:30-04:00",
       "end_local": "2024-07-03T16:30:00-04:00",
       "duration_seconds": 450, "energy": "1.500000"},
      {"window_index": 2,
       "start": "2024-07-03T20:30:00Z", "end": "2024-07-03T20:45:00Z",
       "start_local": "2024-07-03T16:30:00-04:00",
       "end_local": "2024-07-03T16:45:00-04:00",
       "duration_seconds": 900, "energy": "3.000000"}
    ]},
    {"interval_index": 2, "energy": "1.000000", "slices": [
      {"window_index": 8,
       "start": "2024-07-03T22:00:00Z", "end": "2024-07-03T22:15:00Z",
       "start_local": "2024-07-03T18:00:00-04:00",
       "end_local": "2024-07-03T18:15:00-04:00",
       "duration_seconds": 900, "energy": "0.500000"},
      {"window_index": 9,
       "start": "2024-07-03T22:15:00Z", "end": "2024-07-03T22:30:00Z",
       "start_local": "2024-07-03T18:15:00-04:00",
       "end_local": "2024-07-03T18:30:00-04:00",
       "duration_seconds": 900, "energy": "0.500000"}
    ]}
  ],
  "reason_code": null,
  "reason": null,
  "excluded_windows": [
    {"window_index": 0,
     "start": "2024-07-03T20:00:00Z", "end": "2024-07-03T20:15:00Z",
     "start_local": "2024-07-03T16:00:00-04:00",
     "end_local": "2024-07-03T16:15:00-04:00",
     "duration_seconds": 900, "reasons": ["partial_coverage"]},
    {"window_index": 3,
     "start": "2024-07-03T20:45:00Z", "end": "2024-07-03T21:00:00Z",
     "start_local": "2024-07-03T16:45:00-04:00",
     "end_local": "2024-07-03T17:00:00-04:00",
     "duration_seconds": 900, "reasons": ["not_covered"]}
  ]
}
```

获选窗口 `16:15–16:30`（本地）被两段不等长输入各覆盖 7.5 分钟，分摊能量
各为 1.5 kWh（按各自的实际时长比例），合计 3 kWh → 平均功率 12 kW →
费用 12 × 12.50 = **150.000000**；逐输入费用各 75.000000，合计严格等于
总费用。`interval_shares` 则给出每条输入的完整去向：输入 0 的 3.1 kWh
落在窗口 0（1.6，未参评）与窗口 1（1.5），输入 1 的 4.5 kWh 落在窗口 1
（1.5）与窗口 2（3.0），输入 2 的 1 kWh 平分到窗口 8/9——各组之和分别
严格等于 3.1 / 4.5 / 1。`excluded_windows` 只列出与输入范围相交但未参评
的窗口（上例已省略其余缺口窗口）；当**没有任何完整窗口**时 HTTP 仍为
200，`selected_window` 为 `null`、`demand_kw`/`amount` 为 `0.000000`，
并给出 `reason_code="no_complete_window"`（`interval_shares` 仍正常返回）。

### `POST /cost-allocation`

为**一块共享电表后的多名用能方**（2～6 个 ID 唯一的成员）公平拆分
**累计阶梯电费**。每名成员携带与 `/quote` 相同格式的用量区间：
**成员内部区间不得重叠，成员之间允许重叠**（同一时刻本来就是合并计量）。

拆分流程：

1. 取全部成员全部区间的 **UTC 端点并集**，沿 UTC 时间线切成原子半开段；
2. 每名成员的用量按与原子段的**实际 UTC 时长**精确分摊（六位小数，
   舍入残差并入该输入的末段，逐成员能量严格守恒），成员之间在同段的
   负荷直接相加；
3. 对**任意成员联盟**，把其合并后的各原子段（零负荷段跳过）作为可首尾
   相接的区间**复用 `/quote` 计价引擎**报一次价——累计阶梯跨成员共享，
   得到联盟费用 `v(S)`；空联盟费用为 0。

精确枚举全部 `2^n` 个联盟后，每名成员的归属额取其在**所有加入顺序**中
边际增费 `v(S∪{i}) − v(S)` 的平均值，即 **Shapley 值**
（`n ≤ 6`，至多 `6! = 720` 个顺序）。归属金额按**输入顺序**量化到六位
小数（四舍五入），**最后一名取全体费用减去前项之和**——因此各成员归属
金额**严格合计为全体合并报价**。

请求示例（服务端口 **8000**）：

```bash
curl -s -X POST localhost:8000/cost-allocation -H 'Content-Type: application/json' -d '{
  "version": "us-ny-demo-v2024",
  "members": [
    {"member_id": "shop",
     "intervals": [
       {"start": "2024-07-06T10:00:00Z", "end": "2024-07-06T12:00:00Z",
        "timezone": "UTC", "energy": 60}
     ]},
    {"member_id": "flat",
     "intervals": [
       {"start": "2024-07-06T10:00:00Z", "end": "2024-07-06T12:00:00Z",
        "timezone": "UTC", "energy": 60}
     ]}
  ]
}'
```

两家在同一个周六（`leisure`：100 kWh 内 @0.15、300 kWh 内 @0.18）各用
60 kWh：单独报价都是 9.00，合并后 120 kWh = 100×0.15 + 20×0.18 =
**18.60**；两家完全对称，Shapley 值各为 **9.30**（都比独立报价多 0.30，
即高档外溢费用的公平均摊）。响应：

```json
{
  "version": "us-ny-demo-v2024",
  "currency": "USD",
  "energy_unit": "kWh",
  "total_energy": "120",
  "total_amount": "18.600000",
  "coalitions_evaluated": 4,
  "members": [
    {"member_index": 0, "member_id": "shop", "energy": "60",
     "standalone_amount": "9.000000",
     "allocated_amount": "9.300000", "difference": "-0.300000"},
    {"member_index": 1, "member_id": "flat", "energy": "60",
     "standalone_amount": "9.000000",
     "allocated_amount": "9.300000", "difference": "-0.300000"}
  ]
}
```

- `total_energy` / `total_amount`：全体合并用量与合并报价 `v(N)`
  （六位小数）；`coalitions_evaluated` 为枚举的联盟数 `2^n`（含空联盟）。
- 逐成员给出 `energy`（申报用量精确值）、`standalone_amount`（独立报价
  `v({i})`，六位小数）、`allocated_amount`（Shapley 归属额）与
  `difference = standalone_amount − allocated_amount`（正=合并后少付、
  负=为阶梯叠加承担更多）。
- 成员可使用**各自不同的时区**表达区间；DST 日按实际 UTC 时长分摊，
  与 `/quote` 完全一致。合并报价遇到**规则缺口**同样返回 **409
  `rule_gap`**；成员 ID 重复返回 **400 `duplicate_member`**；同一成员
  内部区间重叠返回 **400 `interval_overlap`**。

### `POST /usage-estimate`

电表用量缺失、但账单金额已知时，反推与账单一致的**用量区间**。请求给出
规则版本、一段**不含 `energy`** 的时间区间（与 `/quote` 区间同格式）、
账单金额 `billed_amount`（非负、最多两位小数）与用量搜索上界
`max_energy`（`0 < max_energy ≤ 1000000`、最多六位小数）。

请求示例（服务端口 **8000**）：

```bash
curl -s -X POST localhost:8000/usage-estimate -H 'Content-Type: application/json' -d '{
  "version": "us-ny-demo-v2024",
  "interval": {
    "start": "2024-07-06T00:00:00-04:00",
    "end":   "2024-07-06T04:00:00-04:00",
    "timezone": "America/New_York"
  },
  "billed_amount": "1.50",
  "max_energy": "100"
}'
```

反推规则：

- 假定区间内**功率恒定**：能量按实际 UTC 时长比例分布，与 `/quote`
  对单段区间的分摊完全一致；时间切分（本地午夜 / DST / 规则边界）、
  累计阶梯与金额舍入（分段六位小数、合计四舍五入到分）都复用
  `/quote` 引擎；
- 在 **0.000001 能量单位**的离散网格上，用 Decimal 二分分别定位
  “账单金额 == billed_amount” 平台的**左、右边界**（网格最多
  10¹² 个点，二分约 40 步）；**零用量视为零费用**；
- 账单金额关于用量单调不减（各档单价非负），因此解集必为一段连续平台。

有解时（`solvable=true`）返回平台两端用量、两端完整报价与命中规则：

```json
{
  "version": "us-ny-demo-v2024",
  "currency": "USD",
  "energy_unit": "kWh",
  "billed_amount": "1.50",
  "solvable": true,
  "min_energy": "9.966664",
  "max_energy": "10.033329",
  "matched_rules": ["leisure"],
  "min_quote": {"total_energy": "9.966664", "total_amount": "1.50",
                "matched_rules": ["leisure"],
                "segments": [{"rule_code": "leisure", "tier_ordinal": 1,
                              "energy": "9.966664", "unit_price": "0.15",
                              "amount": "1.495000", "...": "其余字段与 /quote 相同"}]},
  "max_quote": {"total_energy": "10.033329", "total_amount": "1.50",
                "matched_rules": ["leisure"], "segments": ["..."]},
  "quotes_evaluated": 54,
  "reason_code": null,
  "reason": null,
  "lower_neighbor": null,
  "upper_neighbor": null
}
```

- `min_energy` / `max_energy`：目标金额平台的最小 / 最大用量（六位小数）；
  两端再各退一步（±0.000001），账单金额就会偏离目标；
- `min_quote` / `max_quote`：两端用量的**完整 `/quote` 报价**（含逐段
  明细，可对照分段、阶梯与单价）；零用量端为零费用、无计价段的空报价；
- `matched_rules`：两端报价命中规则的并集；`quotes_evaluated` 为二分
  过程中实际执行的报价次数。

无解时（`solvable=false`，HTTP 仍为 200）用 `reason_code` 区分两种形态，
并给出夹住目标金额的**相邻网格点**及其账单金额：

| reason_code | 含义 |
| --- | --- |
| `amount_above_reachable` | 用量达到 `max_energy` 上界时账单金额仍低于目标（目标超过可达上界）；`lower_neighbor` 为上界点及其金额，`upper_neighbor` 为 `null` |
| `rounding_jump` | 账单金额在相邻两个网格点之间跳过目标（单价极高时一个网格步进就越过 1 分）；`lower_neighbor` / `upper_neighbor` 为夹住目标的相邻两点及其金额 |

例如上界 1 kWh 的周六区间最多产生 0.15 USD 的账单，目标 999.00 即
`amount_above_reachable`（`lower_neighbor` 为 `{"energy": "1.000000",
"amount": "0.15"}`）。输入校验沿用 `/quote` 语义：区间非法
（400 `invalid_interval`）、朴素时间落入 DST 缺口/歧义（400）、版本未
知（404）、计价区间遇规则缺口（409 `rule_gap`）；`billed_amount` /
`max_energy` 的符号、上界与小数位数校验失败返回 422。

### `POST /versions/import`

导入一个**完整费率版本**（版本 + 嵌套的 `holidays`、`rules`、`tiers`），
在**单个 SQLite 事务**内写入全部层级——任一校验失败整体回滚，
数据库不会留下半成品。导入成功后版本**立即**可用于
`/quote`、`/quote-diff` 与 `/shift-plan`。

请求示例（服务端口 **8000**）：

```bash
curl -s -X POST localhost:8000/versions/import -H 'Content-Type: application/json' -d '{
  "code": "acme-2024-v1",
  "currency": "USD",
  "energy_unit": "kWh",
  "description": "ACME 2024 费率",
  "holidays": [
    {"day": "2024-07-04", "kind": "holiday", "name": "Independence Day"}
  ],
  "rules": [
    {"code": "night", "name": "夜间",
     "effective_from": "2024-01-01T00:00:00", "effective_to": "2025-01-01T00:00:00",
     "timezone": "America/New_York", "weekdays": "", "day_scope": "all",
     "time_on": "20:00", "time_off": "08:00",
     "tiers": [{"ordinal": 1, "cumulative_cap": null, "unit_price": "0.10"}]},
    {"code": "day", "name": "白天",
     "effective_from": "2024-01-01T00:00:00", "effective_to": "2025-01-01T00:00:00",
     "timezone": "America/New_York", "weekdays": "1,2,3,4,5", "day_scope": "workdays",
     "time_on": "08:00", "time_off": "20:00",
     "tiers": [{"ordinal": 1, "cumulative_cap": "100", "unit_price": "0.20"},
               {"ordinal": 2, "cumulative_cap": null, "unit_price": "0.30"}]}
  ]
}'
```

**规范化与内容摘要**：导入前先按业务键规范化——规则按 `code` 排序、
阶梯按 `ordinal` 排序、日历按日期排序（同日期同类型的重复条目去重）；
数值（`0.20` / `0.2` / `1e2`）、星期掩码（`"5,1,2"` / `[1,2,5]`）、
`HH:MM`（`8:00` / `08:00`）等写法统一为规范形式。对规范 JSON
（键排序、无空白）取 **SHA-256** 作为 `content_digest`，
因此**列表顺序与等价写法的变化都不会改变摘要**。

**幂等语义**（版本内容不可变）：

| 情形 | 结果 |
| --- | --- |
| 首次导入 | **201**，写入全部层级，返回版本内容与 `content_digest` |
| 同 `code`、同摘要重放 | **200**，原样返回首次的结果，不写库 |
| 同 `code`、不同摘要 | **409** `version_conflict`，`details` 含两个摘要 |

**校验**：版本号与规则代码唯一、IANA 时区有效且**全版本一致**、
生效区间（`effective_to > effective_from`，本地朴素时间）与日内窗口
（`HH:MM`，仅 `time_off` 允许 `24:00`）合法、holiday 日期与类型无冲突
（同一日期不得既是 `holiday` 又是 `workday`）、阶梯序号从 1 连续且
累计上限严格递增（中间档不得开放、最后一档必须开放），并复用与
`/quote` 相同的**规则重叠检查**。字段错误一次性全部返回
（**422 `invalid_import`**），每条都带 JSON 定位路径；结构冲突返回
**409 `rule_conflict`**，`details` 为带规则定位的 issues 列表：

```json
{
  "error": {
    "code": "invalid_import",
    "message": "导入内容存在 2 处字段错误，未写入任何数据",
    "details": {
      "errors": [
        {"path": "holidays[1].kind", "code": "holiday_conflict",
         "message": "日期 2024-05-01 的类型冲突：holidays[0] 为 'holiday'，此处为 'workday'"},
        {"path": "rules[0].tiers[1].ordinal", "code": "tier_gap",
         "message": "规则 day 的阶梯序号必须从 1 开始连续（第 2 档的序号为 3）"}
      ]
    }
  }
}
```

### `POST /cycles/{cycle_id}/batches`

账期用量会**分批到达**，本接口提供持久化的**增量结算**：每批提交后都把
账期内全部批次的区间合并起来，用现有计价引擎对账期**整体重算**，累计阶梯
因此自然**贯穿各批次**。状态全部存入 SQLite（Docker 卷 `tariff-data`），
服务重启后可继续提交、重放与对账。

请求体在一批用量区间（`intervals` 与 `/quote` 完全同格式）之外增加三个字段：

| 字段 | 含义 |
| --- | --- |
| `version` | 规则版本号；**首次成功提交后锁定**，后续批次必须相同 |
| `idempotency_key` | 幂等键（同一账期内唯一）；同键同内容返回首次响应 |
| `expected_revision` | 客户端已知的账期 revision；**首次必须为 0**，之后为上一次响应的新 revision |

处理顺序与并发语义：

1. **先判幂等重放，再检查 expected_revision**：同 `cycle_id` + 同
   `idempotency_key` + 同内容（规则版本与区间换算成 UTC 后取 SHA-256，
   区间批内顺序无关）→ 返回**首次响应**（HTTP **200**，不写库、revision
   不变，即便请求带的 expected_revision 已过期也照样重放）；同键**异内容**
   → **409 `idempotency_conflict`**。
2. `expected_revision` 与账期当前 revision 不一致（首次非 0，或落后于他人
   已提交的批次）→ **409 `revision_conflict`**。两个并发请求持同一 revision
   时只有一个生效（SQLite 写锁 + 条件更新 `WHERE revision=expected`），
   落败方收到 409，可读取最新 revision 后重试。
3. 首次成功后 revision 为 **1**，此后每批 **加一**；规则版本锁定，换版本
   → **409 `version_locked`**。
4. 把历史区间与本批区间全部换算成 UTC 后**合并且禁止重叠**（首尾相接
   允许）；跨批重叠 → **400 `interval_overlap`**。
5. 用现有计价引擎对账期整体重算：版本未知 404、结构问题 409
   `rule_conflict`、报价区间出现规则缺口 409 `rule_gap`，语义与 `/quote`
   完全一致。**任何区间冲突、规则缺口或计价失败都不会写入批次、幂等记录
   或 revision**（单事务原子提交）。

首次提交（服务端口 **8000**）：

```bash
curl -s -X POST localhost:8000/cycles/demo-2024-07/batches \
  -H 'Content-Type: application/json' -d '{
  "version": "us-ny-demo-v2024",
  "idempotency_key": "batch-1",
  "expected_revision": 0,
  "intervals": [{
    "start": "2024-07-06T00:00:00-04:00",
    "end":   "2024-07-06T01:00:00-04:00",
    "timezone": "America/New_York",
    "energy": 60
  }]
}'
```

响应（HTTP **201**）：

```json
{
  "cycle_id": "demo-2024-07",
  "version": "us-ny-demo-v2024",
  "currency": "USD",
  "energy_unit": "kWh",
  "revision": 1,
  "batch_amount": "9.00",
  "total_energy": "60",
  "total_amount": "9.00",
  "quote": { "total_energy": "60", "total_amount": "9.00",
             "matched_rules": ["leisure"], "segments": ["…完整 /quote 报价…"] }
}
```

第二批（周六 `leisure`：100 kWh 内 @0.15、300 kWh 内 @0.18），与首批
**首尾相接**、`expected_revision` 取上一批的新 revision：

```bash
curl -s -X POST localhost:8000/cycles/demo-2024-07/batches \
  -H 'Content-Type: application/json' -d '{
  "version": "us-ny-demo-v2024",
  "idempotency_key": "batch-2",
  "expected_revision": 1,
  "intervals": [{
    "start": "2024-07-06T01:00:00-04:00",
    "end":   "2024-07-06T02:00:00-04:00",
    "timezone": "America/New_York",
    "energy": 60
  }]
}'
```

账期整体重算为 120 kWh = 100×0.15 + 20×0.18 = **18.60**；累计阶梯跨批次
切档（首批全在第 1 档，第二批 40 kWh 留第 1 档、20 kWh 进入第 2 档）：

```json
{
  "revision": 2,
  "batch_amount": "9.60",
  "total_energy": "120",
  "total_amount": "18.60",
  "quote": { "matched_rules": ["leisure"], "segments": ["…跨两批的完整报价…"] }
}
```

字段说明：

- `revision`：本次提交成功后的**新 revision**（首次 1，逐批加一）。
- `batch_amount`：**本批金额** = 账期整体重算后的累计金额 − 重算前累计
  金额，再四舍五入到分（上例 18.60 − 9.00 = **9.60**）。注意各批
  `batch_amount` 之和按分四舍五入后可能与最终 `total_amount` 有 ±1 分
  差异（各批独立取整所致）；账期权威总额始终是最后一次响应的
  `total_amount`。
- `total_energy` / `total_amount`：账期**累计总量**与**累计总额**
  （两位小数到分）。
- `quote`：账期**全部区间**合并重算后的完整 `/quote` 报价（逐段字段与
  `/quote` 完全一致），可直接核对规则命中、阶梯区间与每段金额。

幂等重放：用**同一 `idempotency_key` 与完全相同的请求体**（区间顺序可
不同，按 UTC 排序后摘要一致即可）再次提交，HTTP 为 **200** 且响应体与
首次**逐字节一致**，不产生新批次。重启后重放同样有效。

### `GET /cycles/{cycle_id}`

查看账期当前状态：锁定的规则版本、当前 `revision`、累计总量/总额、
各批次摘要（幂等键、内容摘要、本批能量与金额、提交时间）与全部已合并的
UTC 用量区间（带所属 revision、全局序号），用于重启后接续与对账。
账期不存在返回 **404 `cycle_not_found`**。

```bash
curl -s localhost:8000/cycles/demo-2024-07
```

### `POST /backups/{backup_id}`

**服务不暂停**地导出可迁移的 SQLite 快照，供离线核对。调用方指定一个
幂等的 `backup_id`（字母/数字开头，可含 `_`/`-`，至多 128 字符）：

1. 通过 **SQLite Online Backup API** 在**独立连接**上把在线库**分批增量
   复制**到备份目录的临时文件；批次之间主动让出写锁，因此复制期间
   `/quote`、`/cycles/{id}/batches` 等读写请求**照常工作**（WAL + 分批
   让步，无需停报停结算）；
2. 副本完成后，用**另一条独立连接**执行 `PRAGMA integrity_check`，结果
   必须恰为 `ok`；
3. 计算副本文件的 **SHA-256** 与**字节数**；
4. 校验通过后以**原子链接**发布到数据卷的备份目录（Docker 卷内
   `/data/backups/<backup_id>.db`），并登记元数据。

首次发布返回 **201**：

```bash
curl -s -X POST localhost:8000/backups/2026-10-01-recon
```

```json
{
  "backup_id": "2026-10-01-recon",
  "filename": "2026-10-01-recon.db",
  "download_url": "/backups/2026-10-01-recon/download",
  "size_bytes": 110592,
  "sha256": "9f1c…e3",
  "created_at": "2026-10-01T08:00:00+00:00"
}
```

- 副本是**自包含的单文件**（rollback-journal，不携带 WAL/SHM），可直接
  拷出后用任意 SQLite 3 工具打开离线核对；`sha256` 与 `size_bytes` 供
  核对下载内容。
- **幂等与不可变**：同一 `backup_id` 的重放（包括并发请求）返回**首次
  元数据（HTTP 200）**，不重新复制；并发请求最终只发布一个文件；
  备份目录已存在同名文件时拒绝覆盖（**409 `backup_exists`**）。
- **失败不留半成品**：复制异常（500 `backup_failed`）或 integrity_check
  不为 `ok`（500 `backup_integrity_failed`）都会删除临时文件，目录中只
  保留已发布件；服务重启时还会清扫上次崩溃残留的 `.tmp-*` 临时文件与
  缺文件的孤儿登记行。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/backups/{backup_id}` | 发起/重放在线备份（首次 201，重放 200；见上） |
| GET | `/backups/{backup_id}` | 查询已发布备份的元数据（不存在 404 `backup_not_found`） |
| GET | `/backups/{backup_id}/download` | 下载 SQLite 副本（`application/vnd.sqlite3`，响应头 `X-Backup-SHA256`/`Content-Length`） |

非法 `backup_id`（含路径穿越尝试如 `../x`）返回 **400
`invalid_backup_id`**（穿越路径段在路由层即 404）。备份目录默认为
数据库文件同级的 `backups/`，可用环境变量 `BACKUP_DIR` 覆盖。

### 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/versions` | 列出全部版本及其规则/阶梯 |
| GET | `/versions/{code}` | 查看单个版本 |
| GET | `/versions/{code}/issues` | **显式校验**：返回规则时间重叠与阶梯缺口 |
| POST | `/versions/import` | 导入完整版本（单事务落库、SHA-256 内容摘要、幂等重放，见上） |
| POST | `/quote-diff` | 两版本对同一组用量区间独立报价并逐段对比（见上） |
| POST | `/shift-plan` | 两项可移时任务在 8 小时窗口内枚举 30 分钟候选，统一报价选最低费用组合（见上） |
| POST | `/meter-readings/reconcile` | 累计表计读数对账：排序去重、reset 断列、回卷与采样间隔 gap，产出可直提 `/quote` 的用量区间（见上） |
| POST | `/demand-charge` | 不等长用量区间按本地午夜对齐的 15/30/60 分钟窗口结算最大需量：DST 感知枚举、按实际时长精确分摊、逐输入能量守恒、逐输入全窗口切片对账（见上） |
| POST | `/cost-allocation` | 共享电表 2～6 个成员：UTC 端点原子段 + 按实际时长分摊合并，枚举全部联盟以 Shapley 值公平拆分阶梯电费，归属额严格合计为全体报价（见上） |
| POST | `/usage-estimate` | 账单金额已知、用量缺失时反推用量区间：恒定功率假设 + 0.000001 网格 Decimal 二分定位目标金额平台两端，无解时区分超过可达上界与舍入跳跃（见上） |
| POST | `/cycles/{cycle_id}/batches` | 账期分批用量的持久化增量结算：expected_revision 乐观锁 + 幂等重放、UTC 合并且禁重叠、账期整体重算让累计阶梯贯穿各批次（见上） |
| GET | `/cycles/{cycle_id}` | 查看账期当前 revision、累计总量/总额、批次摘要与全部 UTC 区间（见上） |
| POST | `/backups/{backup_id}` | 服务不暂停的在线 SQLite 快照：Online Backup API 分批复制、integrity_check、SHA-256/字节数、原子发布、幂等重放、不可覆盖（见上） |
| GET | `/backups/{backup_id}` | 查询已发布备份的元数据（下载地址、摘要、字节数；见上） |
| GET | `/backups/{backup_id}/download` | 下载已发布的自包含 SQLite 副本（见上） |

## 错误处理

统一响应体：`{"error": {"code": "...", "message": "...", "details": ...}}`。

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 400 | `nonexistent_local_time` | 朴素时间落在 DST 春季跳时缺口 |
| 400 | `ambiguous_local_time` | 朴素时间在 DST 回拨时含义不明确 |
| 400 | `invalid_timezone` / `invalid_datetime` / `invalid_interval` | 时区、时间格式或区间非法（end ≤ start、能量 ≤ 0） |
| 400 | `offset_required` | `/shift-plan` 的任务窗口或 `/meter-readings/reconcile` 的读数/reset 时刻未携带 UTC 偏移量 |
| 400 | `window_too_large` / `duration_exceeds_window` | `/shift-plan` 任务可用窗口超过 8 小时，或持续时长超出可用窗口 |
| 400 | `interval_overlap` | 输入的多个用量区间相互重叠（或 `/cost-allocation` 同一成员内部区间重叠） |
| 400 | `duplicate_member` | `/cost-allocation` 的成员 ID 不唯一 |
| 400 | `reading_out_of_range` | `/meter-readings/reconcile` 的读数值不在 `[0, modulus)` 内 |
| 400 | `conflicting_readings` | 同一时刻出现数值不一致的两条读数 |
| 400 | `reset_not_on_reading` | `resets` 中的时刻没有命中任何读数 |
| 404 | `version_not_found` | 规则版本不存在 |
| 409 | `rule_overlap` | 同一时刻有两条规则同时命中（考虑星期/节假日/日内时段后的**条件重叠**） |
| 409 | `tier_gap` | 阶梯上限不递增、中间档开放、最后一档封闭、无阶梯等 |
| 409 | `rule_gap` | 报价区间内存在任何规则都不覆盖的时刻 |
| 409 | `version_conflict` | `/versions/import` 的同 code 不同内容摘要，版本不可变，拒绝覆盖 |
| 409 | `revision_conflict` | `/cycles/{id}/batches` 的 `expected_revision` 过期（首次非 0，或并发下账期已被其他请求推进）；同 revision 并发只有一个生效，落败方收到此码 |
| 409 | `idempotency_conflict` | 同一账期内幂等键已用于不同的提交内容（同键重放必须与首次完全一致） |
| 409 | `version_locked` | 账期首次提交已锁定规则版本，后续批次提交了不同的 `version` |
| 404 | `cycle_not_found` | `GET /cycles/{cycle_id}` 查询的账期不存在 |
| 400 | `invalid_backup_id` | 备份 ID 不符合 `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`（含路径穿越尝试） |
| 404 | `backup_not_found` | 查询/下载一个尚未发布的备份 |
| 410 | `backup_file_missing` | 有登记行但备份文件在数据卷上缺失（正常不会出现，重启清扫会移除该行） |
| 409 | `backup_exists` | 备份目录已存在同名最终文件，已有备份不可被覆盖 |
| 409 | `backup_in_progress` | 跨进程并发发布的极窄交错中胜者尚未完成登记，稍后重试即可 |
| 500 | `backup_integrity_failed` | 副本 `integrity_check` 不为 `ok` 或打开即报数据库损坏；临时文件已删除 |
| 500 | `backup_failed` | Online Backup 复制失败或元数据登记失败；临时文件已删除 |
| 422 | `invalid_import` | `/versions/import` 字段校验失败；`details.errors` 逐条给出 JSON 定位路径 |
| 422 | `invalid_rule` | 版本内规则字段（时区/时间/档位数值）无法解析 |
| 422 | `incompatible_versions` | `/quote-diff` 的两版币种或能量单位不同，拒绝比较 |

`POST /quote` 在计价前会强制对版本做结构校验；发现重叠或缺口直接返回
**409 并带完整 details**（可一次报出多处问题）。也可用
`GET /versions/{code}/issues` 单独查看。例如两条时间重叠的规则：

```json
{
  "version": "broken-demo",
  "ok": false,
  "issues": [
    {"type": "tier_gap", "rule": "b", "message": "规则 b 的最后一档必须开放……"},
    {
      "type": "rule_overlap",
      "rules": ["a", "b"],
      "at": "2024-06-01T00:00:00+00:00",
      "interval_a": ["2024-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"],
      "interval_b": ["2024-06-01T00:00:00+00:00", "2024-07-01T00:00:00+00:00"],
      "message": "规则 a 与 b 在 2024-06-01T00:00:00+00:00 起时间段重叠……"
    }
  ]
}
```

重叠检测是“条件感知”的：工作日规则与周末规则即便日期范围相同也**不**算重叠；
而 `holidays` 与覆盖节假日的 `non_workdays` 在同一节假日相遇时会被判为重叠。

## 切分算法要点

对全部输入区间取并集后，收集这些切分时刻（全部换算成 UTC）：

1. 每个相关时区（输入时区 + 各规则时区）的**本地午夜**；
2. 各时区的 **DST/UTC 偏移切换瞬间**（按日内偏移变化二分定位到秒）；
3. 规则的生效起止时刻、每日 `time_on`/`time_off`（含跨午夜）；
4. 阶梯按**累计用量**跨档时，再在段内按秒数比例二次切分。

随后逐段唯一定位命中规则（无规则即 `rule_gap`，多规则即 `rule_overlap`），
能量按 UTC 秒数比例分摊（DST 日自然得到 23/25 小时），残差并入每段末片。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
python -m pytest -q
```

测试使用内存 SQLite（`StaticPool`），共 181 个用例，覆盖跨日、春秋 DST、
节假日/调休、跨档切分、重叠与缺口报错、版本对比（原子段对账、
rule/tier/单价变化原因、币种拒绝），以及 `/shift-plan` 的候选枚举、
跨任务共享累计阶梯、同价字典序决胜、规则缺口只淘汰对应候选、
结构错误直接报错与全部输入校验路径，`/versions/import` 的
规范化与摘要稳定性（列表乱序/等价写法）、幂等重放与同 code 冲突、
字段错误定位、单事务原子性与导入后立即可用性，
`/meter-readings/reconcile` 的 UTC 排序与原始下标、同时刻去重/冲突、
一次回卷、reset 断列、采样间隔 gap、静止读数、对账不变量与
接力 `/quote` 的端到端报价，以及 `/demand-charge` 的 15/30/60 分钟
本地午夜对齐、春秋 DST 窗口计数（92/100 个 15 分钟窗、重复本地边界
分别成窗）、跨窗按实际微秒时长精确分摊与逐输入能量守恒、
`interval_shares` 逐输入全窗口切片分组（普通跨窗、DST、无完整窗口
场景下各组能量和严格等于原输入）、同功率取最早 UTC 起点、
首尾相接连续覆盖、边缘/缺口窗口原因、六位小数计费、
无完整窗口的 `no_complete_window` 与全部输入校验路径，以及
`/cost-allocation` 的 UTC 端点原子段切分、按实际时长分摊的逐成员能量
守恒（含跨成员不同时区与春季 DST 23 小时）、同段负荷合并后复用引擎的
联盟报价、对称/非对称 Shapley 值与独立排列枚举对账、末位成员吸收舍入
残差保证归属额严格等于全体合并报价、2～6 成员边界与空联盟、独立报价
对账、重复 ID / 成员内重叠 / 规则缺口等校验路径，以及
`/usage-estimate` 的目标金额平台左右边界（与 `/quote` 逐点对账：两端
命中目标、相邻网格点偏离）、跨阶梯平台、零账单的零用量端、跨规则命中
规则并集、春令时切分一致性、超过可达上界与舍入跳跃两种无解形态
（含相邻网格点）、单点平台与全部输入校验路径，以及
`POST /cycles/{cycle_id}/batches` 的首次 expected_revision=0 与版本
锁定、累计阶梯跨批次贯穿（本批金额=重算前后累计之差）、同键同内容
幂等重放（区间乱序摘要不变、过期 revision 仍重放、HTTP 200）、同键异
内容/旧 revision/换版本冲突、跨批 UTC 区间重叠、规则缺口与非法区间
不写入任何批次/幂等记录/revision、同 revision 并发（线程级两请求、
新建账期竞争与 8 路竞争）只有一个生效且无半成品、重启后状态恢复/
重放/接续提交，以及 HTTP 端到端的 201/200/404/409 响应；在线备份
（`tests/test_backup.py`，临时**文件型** SQLite + 真实写锁）覆盖
Online Backup API 发布自包含副本、独立连接 integrity_check、
SHA-256/字节数与实际文件一致、同 backup_id 重放返回首次元数据、
8 路并发只发布一个文件且无临时残留、备份进行中报价读与结算写持续
成功（12 批全部提交、revision 12）、复制失败/integrity_check 失败
删除临时文件且不留登记行、已有同名文件不可覆盖、非法 backup_id、
重启后元数据/文件仍可下载与重放、启动清扫临时文件与孤儿登记行。

## 目录结构

```
app/
  main.py        # FastAPI 路由与错误处理
  schemas.py    # Pydantic 请求/响应模型
  pricing.py    # 核心引擎：时区/DST/日历/切分/阶梯/校验
  diff.py        # 版本对比：原子段切分、差额与变化原因
  shiftplan.py  # 移时排程：30 分钟枚举 + 统一报价选优
  meter.py      # 表计对账：排序去重、reset 断列、回卷与采样间隔 gap
  demand.py     # 最大需量：本地午夜对齐窗口、DST 枚举、精确分摊与选优
  allocation.py # 共享电表：原子段合并、联盟枚举、Shapley 公平拆分
  estimate.py   # 用量反推：恒定功率假设、离散网格二分定位金额平台
  importer.py   # 版本导入：规范化、SHA-256 摘要、定位校验、单事务落库
  settlement.py # 账期增量结算：幂等重放、expected_revision 乐观锁、
                #   UTC 区间合并、账期整体重算、SQLite 持久化与重启接续
  backup.py     # 在线快照：SQLite Online Backup API 独立连接分批复制、
                #   integrity_check、SHA-256/字节数、原子发布、幂等与
                #   不可覆盖、失败清理与启动清扫
  database.py   # SQLAlchemy 模型（账期/批次/区间/备份表）与预置版本数据
tests/          # pytest 端到端测试
Dockerfile
docker-compose.yml
requirements.txt
```
