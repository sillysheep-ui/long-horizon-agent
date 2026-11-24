# 声明式评测配置

评测配置写在 `evaluation/specs/*.yaml`，一份配置描述一层怎么评：数据集、环境、裁判、五类 grader、阈值、跟踪指标。

| 文件 | 层 | 数据 | 主判定 |
|---|---|---|---|
| `travel_plan.yaml` | 单轮生成 | `product_cases.json` test 110 | 20 条硬约束全过 + rubric ≥0.7 |
| `sandbox.yaml` | 沙盒离线 | 同数据集，10 城子集 36 | 硬约束全过（不跑裁判） |
| `revision.yaml` | 多轮修改 | `product_revision_cases.json` 30 | 保留日不变、目标日生效 |
| `intent.yaml` | 意图路由 | `intent_holdout.json` 40 | 分类准确率 ≥0.95，兜底 ≤0.40 |

## 结构

```yaml
dataset:   {path, split, cases, filters, version_strategy}
environment: {model_profile, poi_provider, seed}
judge:     {model, temperature, repeats, max_tokens}
graders:   [constraint_set | llm_rubric | state_check | tool_calls | transcript]
tracked_metrics: [latency | tokens | transcript]
thresholds: {score_min, deliverable, regression}
```

每个 grader 可带 `id`、`severity`（blocking/scored/info）、`weight`、`threshold` 和 `assertions`。
`assertions` 每项至少要有 `id` 与 `text`，可以再带 `weight`、`veto`、`when` 等参数。

## 五类 grader

| type | 做什么 | 现在是否真跑 |
|---|---|---|
| `constraint_set` | 调 `product/constraints/sets/v1.json` 的 20 条约束 | ✅ |
| `llm_rubric` | 二值 rubric，每条必须引用原句 | ✅ |
| `state_check` | 产物状态自洽（complete/追问/证据引用） | ⚠️ 部分，JSONPath 断言未实现 |
| `tool_calls` | 工具调用不变量（至少一次/禁止调用） | ❌ 仅声明 |
| `transcript` | 回合数、工具调用数、token 上限 | ❌ 仅声明 |

## 校验与使用

```bash
python3 -m evaluation.spec_check evaluation/specs/travel_plan.yaml
python3 -m evaluation.product_eval --spec evaluation/specs/travel_plan.yaml
```

校验：必填字段、数据集/rubric 文件存在、grader 类型与 id 唯一、阈值范围、权重范围、指标类型。
运行时目前只读取 `dataset.path`、`dataset.split`、`thresholds.score_min`，其余字段是给人和 CI 看的契约。

## 和“通用 agent eval YAML”的对应

| 通用写法 | 我们的对应 |
|---|---|
| `graders: llm_rubric` | `plan_quality`（二值 + 引用 + veto 项） |
| `graders: state_check`（tickets/refunds） | `state`（行程状态与证据引用）+ `revision_checks` |
| `graders: tool_calls` | `tools`（poi_search 每天至少一次、禁止未允许工具） |
| `graders: transcript max_turns` | `transcript`（60 回合、18 次调用） |
| `tracked_metrics` | 延迟/分阶段耗时/token；TTFT 暂不做（非流式） |

## 为什么“未实现”也写进配置

配置先成为契约：路径、阈值、断言都在 CI 校验；runner 逐项接。声明了但没实现的项在表里明确标 ❌，避免看起来比实际能力强。

YAML 解析依赖 PyYAML，只在评测环境需要，不进产品依赖；缺失时 `spec_check` 给出安装提示。
