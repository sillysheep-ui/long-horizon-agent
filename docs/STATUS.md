# 项目状态（2026-11-26 收尾）

这份文档是给“接手的人 / 面试官 / 未来的自己”看的一页纸：现在能做什么、数字是什么口径、还差什么。

## 一句话

旅行规划 Agent：从一句中文需求出发，做需求抽取、按天计划、证据检索、覆盖核验、模型排期、评审与修复，输出带来源的多天行程；默认由模型决定活动与时间，代码只做结构契约和规则校验。

## 现在能跑

```bash
# Demo
cp .env.example .env            # 填 OPENAI_API_KEY
uvicorn product.api:app --port 8000

# 主评测（test 120，含裁判）
python3 -m evaluation.product_eval --spec evaluation/specs/travel_plan.yaml \
  --judge-model deepseek-v4-flash

# 回归（8 条历史 bug，CI 门禁）
python3 -m evaluation.product_eval --spec evaluation/specs/regression.yaml \
  --trials 2 --skip-judge --min-pass-rate 1.0

# 多轮修改 / 意图
python3 -m evaluation.revision_eval
python3 -m evaluation.intent_eval

# 静态评测（沙盒快照 + 禁外部检索 + 数据指纹）
python3 scripts/fetch_sandbox.py
python3 -m evaluation.product_eval --static --skip-judge
```

静态模式的判定口径、检查分类与当前边界见 `docs/STATIC_EVAL.md`。

沙盒只覆盖 10 城（北京、上海、广州、深圳、成都、杭州、重庆、武汉、南京、苏州），其余城市走联网。

## 当前数字（口径：对外只报可交付率 / 硬约束全过率）

| 集合 | 结果 | 说明 |
|---|---|---|
| test 120 | 全过 55/120，可交付 40/120，rubric 0.850，18.75s | **用餐冲突修复（2f9a6e7）之前的基线，修复后尚未重跑** |
| 沙盒 41 | 旧口径 40/41；纳入用餐重叠后 25/41（0.61），修复前 | 两次口径不同，引用前先重跑 |
| 回归 8 | 8/8，pass^2 1.0 | 修复前 |
| 多轮 30 | 30/30 | 模型排期默认化之前 |
| 意图 60 | holdout 模型优先 100%，规则兜底 30% | — |

综合分只作为内部信号，`docs/SCORING.md` 有完整口径。

## 已知限制

- 高德 key 无效：联网模式降级网页搜索，POI 名称/地址/营业时间质量下降；live 池通过率明显低于沙盒池。
- 无人工金标准：裁判未校准，rubric 不进对外数字。
- 用餐重叠已改为本地确定性修复、重排只在清零时接受，但没有全量复测。
- 静态评测模式已加（`--static`），但用例仍 41 条、未扩到 100+，也没有冻结静态 holdout。
- 两套校验器（`plan_rules` / `constraints`）未合并；`evidence_id` 是查询级不是地点级。
- 全局选点只到骨架（`PLAN_MODE=global`），回答阶段仍是逐天生成。
- 单 worker 串行、无任务级取消/断点续跑；无鉴权、限流、监控。
- CI 的 eval smoke 因仓库没有 `DEEPSEEK_API_KEY` secret 一直跳过。
- 训练线与产品未打通：产品调 DeepSeek API，GRPO/OPD 是可复现方法、不可复现数值的研究原型。

## 如果继续，优先级

1. 重跑 test 120，拿到用餐修复后的真实数字；
2. 标 50–100 条人工金标准，校准裁判并报一致率；
3. 抽一层任务 runtime（状态机 + 阶段 checkpoint + 取消/恢复），再决定是否接 AgentScope；
4. 合并两套校验器、把 evidence_id 改成地点级。
