# 产品行程质量评测

回答一个问题：**改完提示词或换了模型，行程到底变好还是变差了？**

没有量化，就只能凭“看起来像不像”。这里用三层评测，从便宜到贵，设计参考了三类工作：

- **可验证约束优先**：TravelPlanner（[arXiv:2402.01622](https://arxiv.org/abs/2402.01622)）用程序化约束通过率评行程，而不是让模型“给个印象分”；
- **结构化 rubric 优于 Likert**：Rubrics as Rewards（[arXiv:2507.17746](https://arxiv.org/abs/2507.17746)）把 rubric 当奖励，比直接打 1–5 分更稳、裁判方差更小；
- **先理由后判定**：G-Eval（[arXiv:2303.16634](https://arxiv.org/abs/2303.16634)）证明 chain-of-thought 式评分比直接打分更接近人工。

## 第一层：确定性检查（不花模型调用）

`evaluation/product_checks.py` 对每条行程做 11 项检查，全部来自结构化字段：

| 检查 | 含义 |
|---|---|
| `complete` | 状态是 complete，而不是 partial |
| `day_count` | 天数与需求一致 |
| `density` | 每天 2–3 个活动，不空也不塞 |
| `unique_titles` | 跨天不重复同一地点 |
| `title_clean` | 标题短且不含“攻略/门票/图片”等网页噪声 |
| `time_order` | 同一天时间升序 |
| `fields_filled` | start/duration/transport/cost 都有值 |
| `evidence_ok` | 每个活动都能指回一条 ok 证据 |
| `city_coverage` | 每天的城市都在用户要求的城市里（`杭州` 与 `杭州市` 等价） |
| `title_matches_evidence` | 活动标题能在对应证据的地点名里找到 |
| `no_fabricated_cost` | 活动里出现的数字（票价等）必须能在证据原文里找到 |

这些检查便宜、稳定、可回归，适合放进 CI；`no_fabricated_cost` 是纯程序化的“编造探测器”，也是 RL 奖励里最不该被 hack 的一项。

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

## 综合分与通过线

```
score = 0.6 × 硬检查通过率 + 0.4 × rubric − 0.2 × 是否编造   （截断到 0–1）
```

一条结果同时满足以下三条才算**可交付**：

1. 11 项确定性检查全部通过；
2. rubric ≥ 0.7（10 条里至少对 7 条）；
3. 没有编造数字。

报告里的“判定”列直接按这条规则生成。这个分数同时也是 RL 的 dense reward 骨架：
硬约束部分可复现、不可 hack；rubric 部分覆盖“好不好用”，但权重压在 0.4，避免模型学会只讨好裁判。

## 偏差控制

1. **盲评**：裁判只看到用户需求和行程文本，不知道是哪个模型、哪个 profile 生成的。
2. **引用制**：每条判定必须引用原句，空口判定无法拿分。
3. **同源偏差**：裁判和被测模型可能同源（都是 DeepSeek），分数会偏乐观；关键结论要人工抽检。
   参考 LLM-as-a-Judge 的偏差分析（[arXiv:2306.05685](https://arxiv.org/abs/2306.05685)）与自我偏好研究（[arXiv:2404.13076](https://arxiv.org/abs/2404.13076)）。
4. **双裁判一致性**：`--judge2-model` 可加第二个裁判，报告里记录两名裁判的 rubric 差值，差值大即标记人工复核。

## 第三层：人工抽检（尚未自动化）

抽取 20% 的结果人工核对，重点看裁判高分的条目是否真的可用。这一步用来校准裁判，避免“分数高但没法用”。

## 评测集

`evaluation/product_cases.json` 共 44 条，按用途固定分成三份，**跑之前先看 split，不要拿 test/holdout 调提示词**：

| split | 条数 | 用途 |
|---|---|---|
| `dev` | 12 | 日常迭代：改提示词、改流程时随便跑，允许过拟合 |
| `test` | 24 | 主报告：汇报、版本对比都用这一份，调参时少看细节 |
| `holdout` | 8 | 上线前/对外结论：平时不跑，避免间接调参 |

覆盖维度：

| 维度 | 例子 |
|---|---|
| 时长 | 1 天（西安、天津）、2 天（杭州、苏州）、3–4 天（成都、北京）、5 天（三亚） |
| 距离/城市 | 单城、双城（成都—重庆、昆明—大理）、三城（南京—上海—杭州） |
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

## 当前基线（2026-11-13）

| 配置 | 硬检查 | rubric | 综合分 | 可交付 | 平均耗时 |
|---|---|---|---|---|---|
| api_only（DeepSeek） | 7/8 全过（平均 0.989） | 0.90 | 0.924 | 5/8 | 9.9s |
| local_first（Qwen3-4B） | 4/8 全过（平均 0.875） | 0.58 | 0.683 | 1/8 | 135.6s |

这两行是扩集前的 8 条用例（现在都是 `dev` 的子集），新 44 条的全量基线见下一节。
4B 的主要失败点：`budget-tight` 被误判为追问、`chengdu-3d` 密度不足、`eastchina-3d` 城市跑偏、`xian-1d` 出现证据里没有的价格数字。

api_only 唯一没过的硬检查是 `hangzhou-2d` 的 `title_matches_evidence`：模型把一家咖啡馆写成“青芝坞森系咖啡馆”，
但对应证据里只有“西湖边文艺咖啡馆”，属于“名字对不上来源”，正是这条检查要抓的。

旧版（Likert 1–5，已废弃）基线：api_only 8/8、4.09 分、9.7s；local_first 4/8、2.76 分、136.7s。
两版分数不可直接比较，换 rubric 后所有历史数字都要重跑。

## 已知局限

1. 8 条用例只能发现明显退化，不能证明“普遍更好”；扩到 30–50 条才够做统计。
2. 价格、班次、营业时间仍没有权威数据源（高德 key 无效），`no_fabricated_cost` 只能查“有没有来源”，不能查“来源对不对”。
3. 裁判对“追问”类用例不适用，已跳过打分。
4. 训练侧的任务协议（`<tool_call>`/`<answer>`）与产品 JSON 协议不同，两套 harness 的比较要另建用例。
