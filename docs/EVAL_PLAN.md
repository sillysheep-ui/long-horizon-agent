# 评测规划

这份文档回答三个问题：**评什么、怎么评、评出来的数怎么用。**
目标不是“有个分数”，而是让每次改动都能判断“变好还是变坏”，并且结论经得起追问。

## 0. 一页纸

- 被测对象：一个“需求 → 计划 → 查询 → 核验 → 回答”的长程旅行规划 Agent，外加多轮修改与意图路由。
- 现有资产：单轮 172 条（dev 12 / test 120 / holdout 40）、多轮 30 条、意图 60 条（40 holdout + 20 smoke，另 100 条归档）、20 项确定性检查、10 条二值 rubric、阶段归因。
- 历史基线（原 test 110）：全过 100/110、综合分 0.902；多轮 30/30；意图 holdout 混合 100%（规则 87.5%、兜底 30%）。
- 最大短板：人工金标准还没做、holdout 还没跑、本地 4B 只有 8 条老基线。
- 节奏：先补可信度（人工校准 + 负控制 + holdout），再扩规模，最后接真实流量。

## 1. 评什么：五层被测面

| 层 | 问题 | 主要手段 |
|---|---|---|
| L1 端到端行程 | 这份行程能不能用 | 硬检查 + rubric + 人工 |
| L2 单步能力 | 哪一步出了问题 | 阶段检查 + 归因 |
| L3 多轮交互 | 改一天会不会把别的改坏 | 保留日一致性检查 |
| L4 意图路由 | 用户这句话想干什么 | 规则/混合准确率 + 兜底率 |
| L5 系统 | 快不快、贵不贵、稳不稳 | 延迟、token、错误率、缓存命中 |

L2 的关键是**漏斗**：需求（intake）→ 计划（planning）→ 检索（retrieval）→ 覆盖（coverage）→ 回答（answer）。
一次失败只归因到最早失败的那一步，下游记 `blocked`，避免一个错被重复计数。

## 2. 数据集

### 2.1 现有

| 数据集 | 规模 | 用途 | 文件 |
|---|---|---|---|
| 单轮 dev | 12 | 改提示词时快速看 | `evaluation/product_cases.json` |
| 单轮 test | 120（含 10 条锚定） | 日常回归与发布对比 | 同上 |
| 单轮 holdout | 40 | 只对外报告，平时不跑 | 同上 |
| 多轮 | 30 | 改一天、追问、补充 | `evaluation/product_revision_cases.json` |
| 意图冒烟 | 20 | 每次改路由跑 | `evaluation/intent_smoke.json` |
| 意图 holdout | 40 | 只对外报告 | `evaluation/intent_holdout.json` |
| 意图调参归档 | 100 | 已归档，不再日常跑 | `evaluation/archive/intent_cases_100.json` |

### 2.2 规模怎么定

用置信区间反推，而不是拍脑袋：

| 样本量 | 95% 区间半宽（p≈0.9） |
|---|---|
| 44 | ±9pp |
| 100 | ±6pp |
| 200 | ±4pp |

结论：**做回归 100 条够，做“小幅提升”的结论要 200+，做 RL 奖励要程序化生成 500+。**
人工评测集与合成奖励集必须分开：前者买覆盖与可信，后者买密度。

### 2.3 缺口

- **人工金标准 50 条**：现在只有自动检查，没有“人认为好不好”的锚。
- **负控制 20 条**：故意给错误行程，确认检查能抓住（抓不住说明检查无效）。
- **真实流量 50 条**：上线后从日志采样，按真实分布补集。
- **多轮扩到 50 条**：补“改预算导致天数变化”“连续改两天”“改完再问”。

## 3. 指标与判定

### 3.1 确定性检查（单轮 16 项）

结构类：`complete`、`days_present`、`day_count`、`density`、`unique_titles`、`time_order`、`no_overlap`、`day_load`、`periods`、`activity_length`、`free_time`。
事实类：`title_clean`、`title_matches_evidence`、`evidence_ok`、`no_fabricated_cost`、`named_places_covered`、`city_coverage`、`clarification`。

### 3.2 rubric（10 条二值）

具体性 c1/c2、可执行性 f1/f2、偏好匹配 p1/p2、事实边界 b1/b2、丰富度 r1/r2；每条要求引用原句。

综合分：

```
score = 0.6 × 硬检查通过率 + 0.4 × rubric 得分 − 0.2 × 编造惩罚   （截断到 0–1）
可交付 = 硬检查全过 且 rubric ≥ 0.7 且 无编造
```

### 3.3 多轮（10 项）

`complete`、`days_present`、`day_count`、`applied`、`kept_days`（保留日一字不动）、`target_changed`、`unique_titles`、`evidence_ok`、`time_order`，加上问答/追问分支的 `answered`、`mentions_day`、`no_new_version`、`trip_intact`、`clarified`、`days_empty`。

### 3.4 意图

- `rule_accuracy`：纯规则。
- `final_accuracy`：规则 + 模型兜底 + 低置信度追问。
- `fallback_rate`：触发模型兜底的比例（越高说明规则越不够用）。
- 对外只报 holdout。

### 3.5 过程指标

阶段成功率、失败类型分布、归因来源（model / data / tool / evaluator）、缓存命中率、`reused` 占比、token 与延迟。

## 4. 可信度保障

1. **裁判协议**：5 维锚点（5/3/1）+ 二值清单 + 必须引用原句 + 温度 0。
2. **双裁判**：分歧 > 0.15 的样本进人工复核；报告记录裁判健康度（截断率 >10% 视为不可信）。
3. **人校准**：50 条人工打分，要求 Spearman ≥ 0.6、重复打分标准差 < 0.05。
4. **负控制**：20 条错误行程，要求检查通过率 0%。
5. **统计**：固定 seed；同集对比用配对 McNemar / bootstrap；对外给区间，不给单点。

## 5. 运行矩阵

| 场景 | 数据集 | 频率 | 耗时 | 成本 |
|---|---|---|---|---|
| CI smoke | dev 12 | 每次提交 | 约 2 分钟 | 可忽略 |
| 改提示词 | dev 30 | 每天 | 约 5 分钟 | 低 |
| 发布回归 | test 120 | 每次发布 | 约 18 分钟 | 中 |
| 对外报告 | test 120 + holdout 40 | 里程碑 | 约 25 分钟 | 中 |
| 本地模型对比 | dev 30 | 里程碑 | 约 70 分钟 | 电费 |

模型档位：`api_only`（基线）、`local_first`（4B + 兜底）、`api_first`。
裁判统一 `deepseek-v4-flash`，必要时第二裁判。

## 6. 当前基线（2026-11-17）

| 评测 | 结果 | 延迟 |
|---|---|---|
| 单轮 test 110（api_only，历史基线） | 全过 100/110、硬均值 0.993、rubric 0.882、综合分 0.902、可交付 68/110 | 9.69s |
| 单轮 test 100（修复前） | 全过 88/100、综合分 0.876 | 10.58s |
| 多轮 30（api_only） | 30/30、定向生效 17/27 | 首次 9.37s / 修改 6.01s |
| 意图 holdout 40 | 规则 85%、混合 97.5%、兜底率 30% | 16.3s |
| 本地 4B（8 条，旧 rubric） | 硬检查 4/8、rubric 0.58 | 136.7s |

## 7. 失败归因与改进闭环

1. 跑评测 → 出报告（逐条 + 分阶段 + 归因）。
2. 按“最早失败阶段”分组，取前三个高频错误。
3. 每个错误写一句假设 + 一个最小修复。
4. 修完只重跑失败用例，通过后再跑全量。
5. 坏例进集：把真实失败改写成新用例，防止回退。
6. 每两周做一次“坏例回顾”，淘汰已稳定的用例，避免集子只增不减。

## 8. 上线门禁

- CI：`pytest` 全绿、`ruff` 全绿、smoke 12 条全过。
- 评测 smoke：`.github/workflows/eval.yml` 在每次 push / PR 上跑 dev 12 条，综合分低于 0.80 直接失败；报告作为 artifact 上传。
- 启用方式：仓库 Settings → Secrets and variables → Actions 添加 `DEEPSEEK_API_KEY`；未配置时该 job 自动跳过，不阻塞合并。
- 发布：test 120 综合分不低于基线 2pp、无可交付率下降、无新增编造；holdout 报告随版本归档。
- 对外：必须写清模型档位、seed、裁判模型、样本量与区间。

### 8.1 收尾状态

- 已统一：四套 suite 都写 `report.json`（`evaluation/contract.py`），字段含 suite、主指标、用例数、数据版本哈希、评测器版本、逐条检查；单轮/沙盒/多轮/意图均已接入。
- 联合报告：`python3 -m evaluation.combined --dir results` 生成一张表，并在 CI 的 eval job 里作为 artifact 上传（`results/combined.md`）。
- 例外：多轮那套的 9 项检查仍是 `evaluation/revision_checks.py` 的自定义实现，没有复用约束引擎；原因是它包含 `kept_days`/`target_changed` 这类“修改前后对比”检查，不属于单份行程的约束。已在 `docs/PRODUCT_EVAL.md` 标注，不阻塞发布。
- 待用户操作：仓库 Settings → Secrets 配置 `DEEPSEEK_API_KEY`，否则 eval job 一直走跳过分支。
- 已补：回归/能力两套 spec（`regression.yaml` / `capability.yaml`）；`--trials N` 多次采样与 `pass@1`/`pass@N`/`pass^N`；回归 job 用 `--min-pass-rate 1.0` 硬门禁；回归集改为 8 条历史 bug；负控制 8 类（`tests/test_constraints_negative.py`）。
- 仍未做：裁判重复跑取方差、`comparable=true` 的跨 suite 比较、参考解、人工金标准与 transcript 分诊。

## 9. 三个月路线

**第 1 个月（可信度）**
- 人工打分 50 条，算 Spearman 与重复一致性。
- 负控制 20 条，确认检查能抓住错误。
- 跑完 holdout 40，形成第一份对外报告。
- 本地 4B 跑 test 120，得到真实对比。

**第 2 个月（覆盖）**
- 多轮扩到 50 条（预算变化、连续修改、改完再问）。
- 意图扩到 200 条，补“否定式修改”“口语省略”。
- 成本统计：每次运行的 token 与耗时进报告。

**第 3 个月（真实）**
- 从线上日志采 50 条真实 query，做分布对比。
- 接入天气/交通后补对应检查与用例。
- 对外发布评测报告：方法、基线、局限。

## 10. 已知局限

- 裁判与被测模型同源，存在自偏好偏差，只能靠双裁判与人工校准缓解。
- 单轮 110 条里有一部分是模型生成的，措辞分布不等于真实用户。
- 高德 key 无效时，地点名与坐标质量下降，会系统性拉低 `title_matches_evidence` 与地理相关指标。
- 天气、交通、餐饮深度尚未接入，相关能力现在是“未覆盖”，不是“做得好”。
