# 产品行程质量评测

回答一个问题：**改完提示词或换了模型，行程到底变好还是变差了？**

没有量化，就只能凭“看起来像不像”。这里用三层评测，从便宜到贵：

## 第一层：确定性检查（不花模型调用）

`evaluation/product_checks.py` 对每条行程做 8 项检查，全部来自结构化字段：

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

这些检查便宜、稳定、可回归，适合放进 CI。它们只管“结构对不对”，不管“写得好不好”。

## 第二层：裁判打分（一次模型调用/条）

`evaluation/product_eval.py` 把行程压成文本，交给裁判模型按 5 个维度打 1–5 分。
每档都有行为锚点（写进 `JUDGE_SYSTEM_PROMPT`），避免“凭感觉给分”：

| 维度 | 5 分 | 3 分 | 1 分 |
|---|---|---|---|
| 具体性 `concreteness` | 每个活动都写清时间、时长、具体看点且衔接 | 约一半活动有细节 | 只有地点名 |
| 可执行性 `feasibility` | 顺序合理、交通转场有交代、强度适中 | 有一处明显偏紧或绕路 | 时间线走不完 |
| 偏好匹配 `preference_fit` | 每条偏好（兴趣/预算/同行人/节奏）都有对应安排 | 只覆盖主要偏好 | 答非所问 |
| 事实边界 `fact_boundary` | 未核实价格/时间/班次全标注，零编造 | 若干未标注的确定断言 | 大量编造 |
| 丰富度 `richness` | 每天有变化、有主次、不重复 | 略显单调 | 复制粘贴 |

裁判只看行程文本和证据统计，不补充外部知识；`overall` 取五项均值。

## 通过线

一条结果同时满足两个条件才算**可交付**：

1. 8 项确定性检查全部通过；
2. 裁判均分 ≥ 3.5。

均分 ≥ 4.0 视为好，< 3.0 视为不可用。报告里的“判定”列直接按这条规则生成。

## 第三层：人工抽检（尚未自动化）

抽取 20% 的结果人工核对，重点看裁判给高分的条目是否真的可用。
这一步用来校准裁判，避免“分数高但没法用”。

## 固定场景

`evaluation/product_cases.json` 目前 8 条，覆盖：

- 短途周末（杭州 2 天）、长线（成都 3 天）、单日（西安 1 天）
- 家庭慢节奏（苏州）、多城市跨城（南京—上海—杭州）
- 信息不全（只问建议，应当追问）、低预算（北京 1000 元）、天气备选（厦门雨天）

加用例只需要往 JSON 里加一条，`days` 填期望天数，需要追问的填 `expect: needs_clarification`。

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
```

结果写到 `results/product_eval/<时间戳>-<profile>/`：

- `report.md`：汇总表 + 裁判点评，直接给人看
- `records.json`：逐条原始记录，方便复查和二次分析

`--limit N` 只跑前 N 条，`--skip-judge` 只跑确定性检查（不花钱）。

## 当前基线（2026-11-13，8 条用例）

| 配置 | 确定性检查 | 可交付 | 裁判均分 | 平均耗时 |
|---|---|---|---|---|
| api_only（DeepSeek） | 8/8 | 7/8 | 4.09 | 9.7s |
| local_first（Qwen3-4B） | 4/8 | 0/8 | 2.76（旧版无锚点 rubric） | 136.7s |

## 已知局限

1. 裁判与被测模型可能同源，分数会偏乐观；重要结论要人工抽检校准。
2. 8 条用例只能发现明显退化，不能证明“普遍更好”；扩到 30–50 条才够做统计。
3. 裁判对“追问”类用例不适用，已跳过打分。
4. 没有把训练侧的任务协议跑进来；两套 harness 的比较要另建用例。
