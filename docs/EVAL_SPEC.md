# 声明式评测配置

`evaluation/specs/*.yaml` 描述每一层怎么评。每个 grader 必须标 `status`：

- `implemented`：runner 真的会执行；
- `declared`：只是契约，runner 尚未执行。

## 六份配置

| 文件 | 层 | 已实现 | 仅声明 |
|---|---|---|---|
| `travel_plan.yaml` | 单轮 110 | 20 条硬约束、10 条 rubric | 状态自洽、工具断言、回合上限 |
| `revision.yaml` | 多轮 30 | 6 项修改检查 | — |
| `intent.yaml` | 意图 40 | 五分类准确率 | — |
| `regression.yaml` | 回归 8（历史 bug） | 硬约束、`pass_rate_min: 1.0`、每次 push | — |
| `capability.yaml` | holdout 40 | 硬约束 + rubric，只记录、不卡合并 | — |
| `static.yaml` | 静态 80（另 20 条冻结） | 沙盒快照、禁外呼、数据质量门禁、本地裁判可选 | 人工校准 |

## 回归 vs 能力

- 回归（`regression.yaml`）：跑 `evaluation/regression_cases.json` 的 8 条历史 bug（半天误判、南昌白名单、
  营业时间、点名地点、多城边界、预算冲突、缺天数、偶发编造价格），只卡确定性检查，要求 100% 通过。
- 能力（`capability.yaml`）：holdout 40，带裁判，允许不满分；用于画能力曲线、观察饱和，不拦合并。
- 多次采样：`product_eval --trials N`。每个 trial 用独立 DB、独立 seed；报告 `pass@1`（单次平均）、
  `pass@N`（至少一次通过）、`pass^N`（N 次全过）与逐 trial 通过率，用来区分真回归和模型抖动。

```bash
# 静态：只跑沙盒快照（80 条 test，20 条 holdout 冻结）
python3 -m evaluation.product_eval --static --spec evaluation/specs/static.yaml --skip-judge

# 回归：CI 跑法（2 次采样，必须全过）
python3 -m evaluation.product_eval --spec evaluation/specs/regression.yaml \
  --trials 2 --skip-judge --min-pass-rate 1.0

# 能力：手动/每周跑，只看曲线
python3 -m evaluation.product_eval --spec evaluation/specs/capability.yaml --seed 7
```

## 负控制

`tests/test_constraints_negative.py` 故意构造 8 类错误行程（编造价格、闭馆时间、时间重叠、天数不符、
跨天重复、证据缺失、标题对不上、超出当日时长），断言对应检查必须失败；每类配一条正例，防止检查
退化成“永远失败”。这份测试不调模型，每次 CI 都会跑。

## 字段，以及谁在读

| 字段 | 谁读 | 是否已生效 |
|---|---|---|
| `dataset.path / split` | `product_eval` | ✅ |
| `thresholds.score_min` | `product_eval` 退出码 | ✅ |
| `graders[].type=constraint_set` | 约束引擎 | ✅ |
| `graders[].type=llm_rubric` | 裁判提示词与 rubric 文件 | ✅ |
| `graders[].assertions` | 文档与校验；不驱动执行 | 部分 |
| `graders[].status` | 校验器强制要求 | ✅ |
| `judge.model` | 校验器；运行时仍读环境变量 | 部分 |
| `judge.enabled=false` | 校验器 | ✅ |
| `thresholds.all_passed / final_accuracy` | 人读；runner 未读 | ❌ |

为了不制造“配置幻觉”，上一版里的这些字段已经删掉：`environment`、`dataset.filters`、`version_strategy`、`judge.temperature/repeats/truncation_policy`、`tracked_metrics`、`thresholds.regression`、`deliverable` 之外的嵌套阈值。它们要么没人读，要么已经由报告本身输出。

## 校验

```bash
python3 -m evaluation.spec_check evaluation/specs/travel_plan.yaml
```

检查：必填字段、grader 类型与 `status`、id 唯一、weight/severity 范围、数据集与 rubric 文件存在、assertions 每条有 `text`、阈值范围。

## 驱动一次评测

```bash
python3 -m evaluation.product_eval --spec evaluation/specs/travel_plan.yaml
```

当前只读取 `dataset.path`、`dataset.split`、`thresholds.score_min`；其余字段用于校验与文档，不改变运行行为——这一点在表里逐项写明，避免“看起来很全”。

## 和通用 agent eval 配置的对应

| 通用写法 | 我们 |
|---|---|
| `llm_rubric` + assertions | `plan_quality`，二值 + 引用 + veto |
| `state_check`（tickets/refunds） | `state_check`（状态三态/追问非空），多轮另有一套 |
| `tool_calls` | `tool_calls`（每天一次 poi、点名地点进查询），仅声明 |
| `transcript max_turns` | `transcript`，仅声明 |
| `tracked_metrics` | 报告里已有延迟/分阶段耗时，不再重复配置 |

YAML 解析依赖 PyYAML，只在评测环境需要，不进产品依赖。
