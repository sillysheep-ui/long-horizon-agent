# 声明式评测配置

`evaluation/specs/travel_plan.yaml` 用一份 YAML 描述“这一层要怎么评”：用哪个数据集、哪几类 grader、阈值是多少、跟踪哪些指标。

```yaml
dataset: {path: evaluation/product_cases.json, split: test}
graders:
  - type: constraint_set      # 约束引擎（20 条）
  - type: llm_rubric          # 二值 rubric，引用原句
  - type: state_check         # 产物状态自洽
  - type: tool_calls          # 过程断言（仅声明，runner 未实现）
  - type: transcript          # 回合上限
tracked_metrics:
  - {type: latency, metrics: [latency_mean, stage_latency_mean]}
thresholds: {deliverable: 0.7, score_min: 0.80}
```

## 校验

```bash
python3 -m evaluation.spec_check evaluation/specs/travel_plan.yaml
```

校验内容：必填字段、数据集文件存在、grader 类型合法、rubric 文件存在、阈值范围、路径可解析。

## 驱动一次评测

```bash
python3 -m evaluation.product_eval --spec evaluation/specs/travel_plan.yaml
```

当前只会用配置里的 `dataset.path`、`dataset.split`、`thresholds.score_min`；`--cases / --split / --min-score` 可覆盖。
`graders` 里的 `tool_calls` 与 `transcript` 是**声明**，runner 尚未执行；`constraint_set` 与 `llm_rubric` 由既有实现承担。

## 为什么这样设计

- 配置只描述“评什么、阈值多少”，不描述“怎么跑”，避免把 runner 写死进 YAML；
- 校验先于运行，路径写错、阈值越界在 CI 就会失败；
- 未实现的部分明确写 `note`，不让配置看起来比实际能力强。

YAML 解析依赖 PyYAML，只在评测环境需要，不进产品依赖；缺失时 `spec_check` 会给出安装提示。
