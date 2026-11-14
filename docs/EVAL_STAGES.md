# 分阶段与分支评测设计（v2）

## 1. 要解决的问题

现有评测只看终稿：硬检查 + 裁判分数。它回答“结果好不好”，不回答：

- 好或坏发生在哪一步；
- 是模型写错、工具没数据，还是流程编排的问题；
- 多个工具并发失败时，该记谁的责任。

本设计在终稿之外记录一条执行轨迹（trace），用确定性规则给每个阶段和每个工具分支打分，产出“哪一步、什么错、谁的责任”。

## 2. 原则

1. **只读埋点**：不改阶段行为、不改提示词、不增加模型调用。
2. **规则判定**：阶段判定全部由代码完成，裁判只负责终稿的语义质量。
3. **判不了就 unknown**：缺数据不推测，不把 unknown 记成失败。
4. **先诊断后计分**：阶段分与分支分暂不进入综合分，避免与既有基线不可比。
5. **线上离线同一套**：trace 结构与检查函数同时服务离线评测与线上监控。

## 3. 数据模型：trace 树

`result["trace"]` 是根节点，一次运行为一个 span。span 字段固定：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 阶段名或工具名 |
| `kind` | str | `run` / `stage` / `tool` |
| `status` | str | `ok` `degraded` `warn` `failed` `skipped` `blocked` |
| `latency_ms` | int | 该 span 耗时 |
| `attributes` | dict | 结构化事实，见下 |
| `events` | list | 异常事件：`{"name","reason","tool","day"}` |
| `error_type` | str? | 失败时的错误类型 |
| `children` | list | 子 span |

示例：

```json
{"name": "run", "kind": "run", "status": "degraded", "latency_ms": 11800,
 "children": [
  {"name": "intake", "kind": "stage", "status": "ok",
   "attributes": {"days": 2, "cities": ["杭州"], "days_declared": true}},
  {"name": "planning", "kind": "stage", "status": "ok",
   "attributes": {"days_planned": 2, "per_day_queries": [3, 2], "nodes": 5}},
  {"name": "retrieval", "kind": "stage", "status": "degraded", "latency_ms": 3200,
   "attributes": {"calls": 5, "ok": 4, "empty": 1, "failed": 0, "unavailable": 0,
                  "cache_hits": 1, "critical_path_ms": 2900, "parallel_efficiency": 0.72},
   "children": [
     {"name": "poi", "kind": "tool", "status": "ok", "day": 1, "latency_ms": 900,
      "attributes": {"cache_hit": false}},
     {"name": "weather", "kind": "tool", "status": "failed", "day": 1, "latency_ms": 2900,
      "error_type": "timeout", "attributes": {"retries": 1}}
   ]},
  {"name": "coverage", "kind": "stage", "status": "warn",
   "attributes": {"per_day_ok": [2, 1], "uncovered_days": [2], "risk": 0.35}},
  {"name": "answer", "kind": "stage", "status": "ok",
   "attributes": {"raw": 6, "grounded": 5, "dropped": 1, "filled": 1, "final": 6,
                  "fabricated_cost": 0, "time_order_ok": true}}
 ]}
```

`status` 语义：

- `ok`：正常完成；
- `degraded`：完成但有降级（降级数据源、缓存复用、重试后成功）；
- `warn`：指标越界但不致命，例如覆盖度有空洞；
- `failed`：该阶段没有产出可用结果；
- `skipped`：条件不适用（例如追问用例不进入检索）；
- `blocked`：依赖上游失败，本节点未执行。

## 4. 金标准

不构造“期望行程”，只做约束级金标准：

| 来源 | 用途 |
|---|---|
| `case.days` | 天数必须一致 |
| `case.cities` | 目的地必须覆盖 |
| `case.expect` | 追问类必须追问，行程类必须给行程 |
| 规划约束 | 天数、城市不越界、每天查询数、无重复查询 |

天气、住宿、交通分支当前未接入，检查记为 `skipped`，不计入失败。

## 5. 检查项

| 阶段 | 检查 | 通过条件 | 错误类型 |
|---|---|---|---|
| intake | 目的地 | 行程类 destinations 非空 | `wrong_city` |
| | 天数 | `constraints.days == case.days` | `missing_days` |
| | 城市 | `case.cities ⊆ destinations` | `wrong_city` |
| | 追问 | `expect=clarify` ↔ `status=clarify` | `should_clarify` / `over_clarify` |
| planning | 天数 | `len(per_day_queries) == days` | `day_mismatch` |
| | 每天够查 | 每天 ≥ 2 个查询 | `too_few_queries` |
| | 城市不越界 | 计划城市 ⊆ 目的地 | `city_drift` |
| | 查询不重复 | 同天关键词无重复 | `duplicate_query` |
| retrieval | 成功率 | `ok / calls ≥ 0.8` | `tool_error` |
| | 无配置缺失 | `unavailable == 0` | `auth_missing` |
| | 关键路径 | p95 ≤ 30s，否则 warn | `timeout` |
| 分支 | 每天景点 | 每天 ≥ 1 条 ok | `uncovered_day` |
| | 每过夜日住宿 | ≥ 1 条（未接入 → skipped） | `missing_hotel` |
| | 跨城日交通 | ≥ 1 条（未接入 → skipped） | `missing_transport` |
| | 每天天气 | 1 条且 ≤ 24h（未接入 → skipped） | `missing_weather` |
| coverage | 无空洞 | `uncovered_days == []` | `uncovered_day` |
| | 每天有料 | 每天 ≥ 1 条 ok | `thin_evidence` |
| answer | 落证据 | `grounded / raw ≥ 0.9` | `ungrounded_title` |
| | 无编造 | `fabricated_cost == 0` | `fabricated_cost` |
| | 密度 | 每天 2–3 个活动 | `density_miss` |
| | 时间有序 | 每天 start 升序 | `bad_time_order` |
| 后处理 | 丢弃率 | `dropped / raw ≤ 0.3` | `density_miss` |
| | 补足数 | 记录，不做判定 | — |

## 6. 错误类型枚举

| 阶段 | 类型 |
|---|---|
| intake | `missing_days` `wrong_city` `missed_budget` `should_clarify` `over_clarify` |
| planning | `too_few_queries` `city_drift` `duplicate_query` `day_mismatch` `planner_parse_error` |
| retrieval | `tool_error` `timeout` `empty_result` `fallback_web` `auth_missing` |
| 分支 | `missing_weather` `missing_hotel` `missing_transport` `stale_evidence` |
| coverage | `uncovered_day` `thin_evidence` |
| answer | `ungrounded_title` `fabricated_cost` `missing_fields` `bad_time_order` `duplicate_place` `density_miss` |
| evaluator | `judge_truncated` `judge_parse_error` `judge_unstable` |

## 7. 归因算法

输入：trace、检查结果、用例。输出：主因、置信度、贡献因素、建议动作。

1. **收集症状**：失败 span、工具异常事件、blocked 节点、未落地标题、编造价格、裁判异常。
2. **过滤 blocked**：`blocked` 节点不投票，归因沿 `blocked_by` 指向上游。
3. **加权投票**：

| 症状 | 归到 | 权重 |
|---|---|---|
| `tool_error` / `timeout` / `auth_missing` | tool | 2 |
| 每天查询不足 / 城市跑偏 | model | 2 |
| 需求字段错、该问没问 | model | 2 |
| 证据为空但工具正常 | data | 2 |
| 标题未落证据 / 编造价格 | model | 1 |
| 用例本身矛盾 | case | 2 |
| 裁判截断 / 解析失败 | evaluator | 2 |

4. **结论**：主因 = 权重最高项；置信度 = 主因权重 / 总权重；其余为 `contributing`。
5. **建议动作**：tool → 查接口/key/超时；model → 改提示词或换模型；data → 补数据源；case → 修用例；evaluator → 换裁判或加预算；orchestrator → 查调度与依赖。

## 8. 三层判定

| 层 | 内容 | 用途 |
|---|---|---|
| `outcome` | 硬检查 + 裁判 + 综合分 | 对外结论 |
| `trajectory` | 阶段分、分支覆盖率、关键路径 | 定位问题 |
| `evaluator` | 裁判截断率、解析失败率、重复判定分差 | 判断结论可不可信 |

`evaluator` 不达标（截断率 > 10% 或重复分差 > 0.15）时，`outcome` 标为“不可用”，不能对外引用。

## 9. 报告

```
outcome     硬检查 92/100  综合分 0.90
trajectory  intake 0.92 | planning 0.71 | retrieval 0.88 | coverage 0.85 | answer 0.74
branches    景点 100% | 天气 skipped | 住宿 skipped | 交通 skipped
attribution model 21 | tool 6 | data 4 | case 2
top errors  too_few_queries 18 | ungrounded_title 5 | uncovered_day 4
evaluator   截断 2 / 解析失败 0 / 重复分差 0.03 → 可信
```

另附“天 × 分支”矩阵：

```
          景点   天气   住宿   交通
day1 杭州  ok     skip   skip   -
day2 杭州  ok     skip   skip   -
```

## 10. 并发与分支

`ToolDAGScheduler(max_concurrency=4, fail_fast=False)` 的节点按依赖并发执行：

- 每个工具调用一个子 span，带 `tool`、`day`、`cache_hit`、`fallback`、`retries`；
- 检索阶段聚合：调用数、成功/空/失败/不可用、缓存命中；
- 耗时看 `critical_path_ms`（最长依赖链）而不是节点耗时之和；
- `parallel_efficiency = 节点耗时之和 / (墙钟 × 并发上限)`，用于判断是否被单个慢节点拖住；
- 依赖失败的节点记 `blocked`，不重复计入失败。

## 11. 接入点

| 文件 | 改动 |
|---|---|
| `product/orchestrator.py` | 每个阶段记录 span；检索阶段按节点记录子 span；记录后处理统计 |
| `evaluation/stage_checks.py`（新增） | span → 检查 → 阶段分 → 归因，纯函数 |
| `evaluation/product_eval.py` | records 带 trace；报告增加 outcome/trajectory/evaluator 三节与矩阵 |
| `tests/test_stage_checks.py`（新增） | 合成 span 单测 |

## 12. 兼容与成本

- 不改表结构、不改接口、不改提示词；
- 旧 records 没有 trace：所有阶段检查为 `unknown`，总分不变；
- 单次评测额外开销为纯计算，网络调用不变。

## 13. 验证顺序

1. 合成 trace 单测，不调模型；
2. 20 条小样本，核对归因是否符合直觉、阈值是否合理；
3. 100 条全量，与旧基线对比；
4. 稳定后再考虑把阶段分纳入综合分。

## 14. 未决事项

- 阶段分与综合分的权重（本版不纳入）；
- 天气/住宿/交通接入后各分支的“应有”规则；
- `evaluator` 健康度阈值的最终取值；
- 线上 trace 的采样率与存储位置。
