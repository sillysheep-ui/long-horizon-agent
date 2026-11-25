# 产品行程质量评测

回答一个问题：**改完提示词或换了模型，行程到底变好还是变差了？**

没有量化，就只能凭“看起来像不像”。这里用三层评测，从便宜到贵，设计参考了三类工作：

- **可验证约束优先**：TravelPlanner（[arXiv:2402.01622](https://arxiv.org/abs/2402.01622)）用程序化约束通过率评行程，而不是让模型“给个印象分”；
- **结构化 rubric 优于 Likert**：Rubrics as Rewards（[arXiv:2507.17746](https://arxiv.org/abs/2507.17746)）把 rubric 当奖励，比直接打 1–5 分更稳、裁判方差更小；
- **先理由后判定**：G-Eval（[arXiv:2303.16634](https://arxiv.org/abs/2303.16634)）证明 chain-of-thought 式评分比直接打分更接近人工。

## 第一层：确定性检查（不花模型调用）

`evaluation/product_checks.py` 对每条行程做 16 项检查（行程带 `meals` 时再加 1 项 `meal_grounded`），全部来自结构化字段。其中 `density`、`day_load`、`activity_length`、`free_time` 按两层信息判断：① 每天可用时长 `constraints.day_windows`（半天约 6 小时、大半天约 8 小时、全天约 10 小时；用户点名某天则覆盖那天）；② 用户节奏（`product/pace.py` 的 relaxed/normal/packed，从 query 关键词识别）。节奏只管“每件事干多久”：relaxed 每个活动 2–3.5 小时、packed 1–1.5 小时，活动数量由可用时长与活动时长共同决定（半天窗口最多 2 个）：

| 检查 | 含义 |
|---|---|
| `complete` | 状态是 complete，而不是 partial |
| `day_count` | 天数与需求一致 |
| `density` | 活动数落在当天的时长区间（半天 ≤2；全天见 pace 档位） |
| `unique_titles` | 跨天不重复同一地点 |
| `title_clean` | 标题短且不含“攻略/门票/图片”等网页噪声 |
| `time_order` | 同一天时间升序 |
| `no_overlap` | 前一项 start + duration 不晚于后一项 start |
| `periods` | 按活动占用区间，每天覆盖上午/下午/晚上中的至少两个时段 |
| `day_load` | 活动总时长不超过当天可用时长、且不少于 3 小时（可用时长不足 3 小时时按可用时长） |
| `activity_length` | 每个活动的停留时长落在 pace 档位内（relaxed 2–3.5 / normal 1.5–2.5 / packed 1–1.5 小时，±1 小时容差） |
| `free_time` | 一天里未被活动占用的时间占比符合 pace（relaxed ≥0.18 / normal 0.10–0.65 / packed ≤0.55） |
| `fields_filled` | start/duration/transport/cost 都有值 |
| `evidence_ok` | 每个活动都能指回一条 ok 证据 |
| `city_coverage` | 每天的城市都在用户要求的城市里（`杭州` 与 `杭州市` 等价） |
| `title_matches_evidence` | 活动标题能在对应证据的地点名里找到 |
| `no_fabricated_cost` | 活动里出现的数字（票价等）必须能在证据原文里找到 |
| `meal_grounded` | 餐饮槽位的名字能对上 ok 证据的地点名（仅行程带 `meals` 时检查） |
| `anchor_satisfied` | 用例声明 `anchor=food` 时每天至少一顿餐、`anchor=stay` 时每天有住宿区域（仅锚定用例检查） |

这些检查便宜、稳定、可回归，适合放进 CI；`no_fabricated_cost` 是纯程序化的“编造探测器”，也是 RL 奖励里最不该被 hack 的一项。

锚定用例已并入主集（10 条：美食 4 / 住宿 3 / 默认 3），带 `tags: ["anchor", "anchor:food"]`，
用 `--ids` 或按 tag 过滤即可单独跑。

## 第二层：rubric 裁判（一次模型调用/条）

`evaluation/product_judge.py` 让裁判回答 10 个**是/否**问题，每条要求引用行程原句（先理由、后判定），
分数 = 通过数 / 10，而不是 1–5 的 Likert 分：

| 维度 | 条目 |
|---|---|
| 具体性 `concreteness` | c1 每个活动有时间与时长；c2 描述写了具体看什么/做什么，不是空话 |
| 可执行性 `feasibility` | f1 当天顺序与转场走得通；f2 单日强度适中 |
| 偏好匹配 `preference_fit` | p1 提到的兴趣都有安排；p2 预算/同行人/节奏被体现 |
| 事实边界 `fact_boundary` | b1 未核实的价格/时间/班次都标注；b2 没有编造具体数字 |
| 丰富度 `richness` | r1 不同日期有明显差异；r2 每天有主次，不是罗列 |

`b2` 不通过时记 `fabrication=true`，综合分额外扣 0.2。

## 评分口径

对外主指标是 **可交付率** 与 **硬约束全过率**，不是综合分。
综合分只在内部做版本排序，完整定义见 [`docs/SCORING.md`](SCORING.md)。

- 确定性检查：`product/constraints/sets/v1.json`，当前 20 条；条件不足记为 unknown 并跳过。
- 可交付：所有适用检查通过 + rubric ≥ 0.7 + 无编造（裁判开启时）。
- 内部排序分：`score_signal = 0.6 × signal + 0.4 × rubric − 0.2 × 编造`，门槛失败封顶 0.2。

历史综合分（`0.6×hard + 0.4×rubric − 0.2×编造`）只用于和旧报告对账，新结论不再引用。

## 偏差控制

1. **盲评**：裁判只看到用户需求和行程文本，不知道是哪个模型、哪个 profile 生成的。
2. **引用制**：每条判定必须引用原句，空口判定无法拿分。
3. **同源偏差**：裁判和被测模型可能同源（都是 DeepSeek），分数会偏乐观；关键结论要人工抽检。
   参考 LLM-as-a-Judge 的偏差分析（[arXiv:2306.05685](https://arxiv.org/abs/2306.05685)）与自我偏好研究（[arXiv:2404.13076](https://arxiv.org/abs/2404.13076)）。
4. **双裁判一致性**：`--judge2-model` 可加第二个裁判，报告里记录两名裁判的 rubric 差值，差值大即标记人工复核。

## 第三层：人工抽检（尚未自动化）

抽取 20% 的结果人工核对，重点看裁判高分的条目是否真的可用。这一步用来校准裁判，避免“分数高但没法用”。

## 评测集

`evaluation/product_cases.json` 共 172 条（手工 + 生成后逐条校验，含 10 条锚定用例），按用途固定分成三份，**跑之前先看 split，不要拿 test/holdout 调提示词**：

| split | 条数 | 用途 |
|---|---|---|
| `dev` | 12 | 日常迭代：改提示词、改流程时随便跑，允许过拟合 |
| `test` | 120 | 主报告：汇报、版本对比都用这一份，调参时少看细节 |
| `holdout` | 40 | 上线前/对外结论：平时不跑，避免间接调参 |

覆盖维度：

| 维度 | 例子 |
|---|---|
| 时长 | 1 天（西安、天津）、2 天（杭州、苏州）、3–4 天（成都、北京）、5 天（三亚） |
| 距离/城市 | 单城、双城（成都—重庆、昆明—大理）、三城（南京—上海—杭州），共 46 个目的地 |
| 同行人 | 独自、情侣、带老人（杭州）、带孩子（上海、三亚、桂林） |
| 预算 | 紧（北京 1000 元、大连 1200 元）、宽（三亚 8000 元） |
| 偏好 | 美食、博物馆、自然、摄影、夜生活、徒步 |
| 天气 | 厦门雨天备选、哈尔滨冬季 |
| 异常输入 | 信息不全（2 条）、矛盾约束（3 天 5 城，1 条） |

每条用例的字段：

- `query`：用户原话；
- `days`：期望天数（追问类可以不填）；
- `cities`：允许出现的城市，供 `city_coverage` 检查；
- `split` / `difficulty` / `tags`：分组与统计用；
- `expect`：只有“应当追问”的用例填 `needs_clarification`。

加用例只要追加一条，`tests/test_product_cases.py` 会校验字段、id 唯一和 split 数量。
多轮、工具使用的评测可参考 TravelBench（[arXiv:2512.22673](https://arxiv.org/abs/2512.22673)）。

## 怎么跑

```bash
set -a; . "/Users/sheep/work/study/项目三/代码/travel_agentic_rl/.env"; set +a

# 云端基线
python3 -m evaluation.product_eval --profile api_only

# 本地 Qwen3-4B
LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1 \
LOCAL_MODEL_NAME="<checkpoint 绝对路径>" \
MODEL_TIMEOUT_SECONDS=240 \
python3 -m evaluation.product_eval --profile local_first

# 双裁判一致性
python3 -m evaluation.product_eval --profile api_only --judge2-model deepseek-chat
```

结果写到 `results/product_eval/<时间戳>-<profile>/`：

- `report.md`：汇总表 + 未通过条目，直接给人看
- `records.json`：逐条原始记录（含 rubric 明细），方便复查和二次分析

`--limit N` 只跑前 N 条，`--skip-judge` 只跑确定性检查（不花钱）。

## 当前基线（2026-11-25）

| 配置 | 全过 | rubric | 综合分 | 可交付 | 平均耗时 |
|---|---|---|---|---|---|
| test 120，模型排期（默认 `PLAN_MODE=model`） | 55/120 | 0.850 | 0.871 | 40/120 | 18.75s |

上下文：`pools = {sandbox: 41, live: 77, unknown: 2}`，裁判 `deepseek-v4-flash`，约束集 1.3.0。

这版数字比旧的 110 条基线低，主要原因不是模型退化，而是 **`no_overlap` 把用餐和活动放进了同一条时间线**：

- 112 条可判用例里，`no_overlap` 通过率 0.491，57 条失败；
- 其中 48 条是“在不同地点同时吃饭和游玩”的真冲突，20 条是同地点用餐；
- 重排能把单条的规则违规从 21 处压到 7 处，但不保证清零。

旧基线把用餐排除在重叠检查之外，所以这部分问题一直没被计入。现在的处理是：清洗后用
`resolve_meal_overlaps` 把冲突的餐挪到当天空档（挪不开就删掉这一餐），只动餐、不动活动；
模型重排只在把硬伤清零时才接受，且重排的 `max_tokens` 从 3000 降到 1800。

## 已知局限

1. 8 条用例只能发现明显退化，不能证明“普遍更好”；扩到 30–50 条才够做统计。
2. 价格、班次、营业时间仍没有权威数据源（高德 key 无效），`no_fabricated_cost` 只能查“有没有来源”，不能查“来源对不对”。
3. 裁判对“追问”类用例不适用，已跳过打分。
4. 训练侧的任务协议（`<tool_call>`/`<answer>`）与产品 JSON 协议不同，两套 harness 的比较要另建用例。

## 完整规划

数据集规模、可信度保障、运行矩阵与三个月路线见 `docs/EVAL_PLAN.md`。

## 附：多轮检查为何没有并入约束引擎

多轮的 9 项检查（`evaluation/revision_checks.py`）里有 `kept_days`、`target_changed`、`applied` 这类**修改前后对比**的判定，输入是两次行程而不是单份行程；约束引擎面向单份行程，强行并入会让算子签名变复杂、收益有限。因此多轮这套保持自定义实现，只在报告信封上与其它 suite 对齐。
