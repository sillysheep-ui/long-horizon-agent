# 声明式行程约束层

评测里原来有 20 多个布尔变量堆在一个函数里，运行时规则又散在 `plancheck.py`、`crosscheck.py`、`revcheck.py`。
同一件事（例如“时间不能重叠”）写了两三遍，加一条检查要改代码、补测试，报告里还看不出差多少。

这层把约束变成**数据**：约束写在 JSON 里，引擎统一求值，输出带 `expected/actual` 的 `Finding`。

## 结构

```
product/constraints/
  model.py      Finding / Constraint / ConstraintSet / ItineraryView
  ops.py        算子注册表（12 个）
  engine.py     build_view / evaluate / passed / failed_ids / summary
  spec.py       validate / load_set
  sets/v1.json  约束集（当前 1.1.0，10 条约束）
```

`evaluation/product_checks.py` 的 `check_result` 现在由两部分拼成：

1. `_legacy_checks`：尚未迁移的检查；
2. `engine_checks`：约束集里声明的检查，**覆盖**同名的旧结果。

迁移期间用 `tests/test_constraints_parity.py` 对本地 39 次评测、1013 条记录逐条比对，
要求引擎与旧实现完全一致（当前 0 mismatch）。

## 一条约束

```json
{
  "id": "opening_hours",
  "category": "commonsense",
  "severity": "hard",
  "type": "time_window",
  "params": {"start_field": "start", "duration_field": "duration",
             "open_field": "opentime", "close_field": "endtime"}
}
```

- `category`：`hard` / `commonsense` / `experience`，用于按类统计；
- `severity`：`hard` 失败即不可交付，`soft` 只提示；
- `type`：算子名，未知算子直接报错，不会静默通过。

一条结果：

```json
{"id": "opening_hours", "ok": false,
 "detail": "陶陶居正佳店 09:30 早于营业时间 10:00",
 "expected": ">= 10:00", "actual": "09:30"}
```

## 算子

| 算子 | 说明 |
|---|---|
| `count_between` | 每天数量区间；`bounds=day_windows` 时按节奏动态取界 |
| `time_order` | 同一天按开始时间升序 |
| `no_overlap` | 活动/用餐不重叠；可选要求时间存在、严格递增 |
| `time_window` | 落在营业时间内 |
| `sum_le` | 每天总时长落在区间内 |
| `exists_contains` | 点名地点必须出现 |
| `status_equals` | 结果状态 |
| `count_equals` | 天数与需求一致 |
| `unique_values` | 标题不重复 |
| `text_clean` | 标题长度与噪声词 |
| `city_coverage` | 每天城市属于需求 |
| `evidence_ok` | 每个活动绑定 ok 证据 |

原则：能声明就声明；声明不了用 `custom` 逃生口，不把引擎扩成通用语言。

## 加一条约束

1. 在 `sets/v1.json` 里加一条，`type` 用现有算子；参数不够就加算子（配单测）；
2. 跑 `python3 -m pytest tests/test_constraints_parity.py -q`，确认没有改变旧判定；
3. 若这条检查此前在 `_legacy_checks` 里，删除对应分支，让引擎结果生效。

约束集有 `version`，报告会写 `约束集 v1.3.0`，历史分数不可比时能看出是哪一版。

## 指标

- **micro**：所有用例的约束通过数 / 约束总数；
- **macro**：整条行程全部检查通过的比例；
- 报告里同时给 `checks_micro` 与 `checks_macro`，避免“平均通过率”掩盖个别用例全崩。

## 条件检查与 unknown 语义

检查分三类：

1. **无条件**：`complete` / `density` / `day_load` 等，永远出现；数据不足按失败算（例如空行程）。
2. **有数据才判**：`opening_hours`（没有营业时间）、`meal_grounded`（没有餐饮槽位）——不出现，不判失败。
3. **用例开启才判**：`anchor_satisfied`（`case.anchor` 是 food/stay）、`named_places_covered`（`case.named_places` 非空）——不适用时省略。

实现方式：算子返回 `meta={"skip": True}`，引擎跳过该键。这样报告里不会出现“没有数据却判失败”的假阴性，代价是不同用例的约束总数可能不同（micro 按各自总数算）。

## 为什么不做完整 DSL

- 规则是封闭集合，20 多条约束覆盖全部检查，完整 DSL 需要词法/语法/AST/错误定位（1.5–3k 行）；
- 参考 Drools 的教训：规则引擎容易变成难调试的黑盒；
- ChinaTravel 用 DSL 是因为它要处理开放自然语言的组合约束，场景不同。

## 参考

- SICs（PVLDB 2025，[arXiv:2503.00600](https://arxiv.org/abs/2503.00600)）：对 LLM 输出声明式规定正确性条件；
- ChinaTravel（ICLR 2026）：组合式约束、硬/常识约束分层、C-LPR；
- CheckList（ACL 2020）：测试规格与实现分离；
- OPA：policy-as-code、规则版本化；
- Drools：规则外置的实践与维护成本警示。

引用边界：以上工作是设计灵感来源，不构成“论文证明本方案有效”；有效性的证据是本地并行对比与测试。
