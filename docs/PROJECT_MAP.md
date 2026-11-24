# 项目地图

一页纸说清：这是什么、能跑什么、现在到哪、下一步干什么。

## 1. 一句话

用户说“上海出发去杭州玩两天、1500 预算”，Agent 输出一份**带来源的**行程；背后有一套研究侧的规划/训练链路和一套可复现的评测。

## 2. 两条线

```
long-horizon-agent
├─ product/          产品线：网页 + HTTP API（能跑）
│   ├─ api.py        FastAPI 入口
│   ├─ workflow.py   需求 → 规划 → 查询 → 核验 → 回答 → 收尾
│   ├─ intent.py     新行程 / 改行程 / 提问 / 闲聊
│   ├─ store.py      SQLite：会话、行程版本、任务、偏好
│   ├─ memory.py     会话摘要、长期偏好
│   └─ constraints/  规则手册（评测与运行时共用）
├─ web/src/          React 单页：对话 + 行程 + 证据
├─ evaluation/       质检：四套 suite + 联合报告
├─ inference/        研究侧：Tool DAG、Verifier、ReAct
├─ tools/            11 个旅行工具 + 高德/网页搜索/沙盒
├─ training/ data_pipeline/  SFT / GRPO 与数据构造
└─ docs/             设计、评测、部署、约束说明
```

## 3. 怎么跑

```bash
# 依赖与密钥
cp .env.example .env        # 填 DEEPSEEK_API_KEY；本地模型另配 LOCAL_MODEL_BASE_URL

# 网页（8000）
set -a; . ./.env; set +a
MODEL_PROFILE=api_only TRAVEL_DB_PATH=./data/agent.db \
  python3 -m uvicorn product.api:app --host 0.0.0.0 --port 8000

# 测试与静态检查
python3 -m pytest tests -q
python3 -m ruff check .
```

模型档位：`api_only`（只用 DeepSeek）、`local_first`（本地 MLX 优先）、`local_only`（只走本地，失败不降级）。

Docker：`Dockerfile` + `docker-compose.yml`；本机没有 Docker Desktop 时用 `deploy/lima.yaml`（见 `docs/DEPLOY.md`）。

## 4. 产品流水线

| 阶段 | 做什么 | 确定性兜底 |
|---|---|---|
| 意图 | 判新行程/改/问/闲聊 | 模型优先，规则兜底取消/撤销 |
| 需求 | 抽城市、天数、预算、偏好 | 缺天数/预算冲突/城市太多 → 追问 |
| 规划 | 输出 ToolPlan（DAG） | 白名单、天数校验、天数/节点上限 |
| 查询 | 高德/沙盒/网页搜索 | 失败降级、单节点失败只标一格、24h 复用 |
| 核验 | 按天覆盖、跨槽位、预算 | 未覆盖日补查一次 |
| 回答 | 生成行程 JSON | 标题回退证据名、时间顺延、补足/排序 |
| 收尾 | 评审 + 最多一次重排 | 硬伤必改，语义问题按分布决定 |

长程能力：改某天（其余不动）、改城市/预算/节奏（整单重排）、顺延、撤销、取消、幂等（60s 去重）。

## 5. 四套评测

| suite | 数据 | 主指标 | 最近结果 |
|---|---|---|---|
| 单轮 | `evaluation/product_cases.json` 162 条（test 110） | 综合分 0.6×硬检查+0.4×rubric−编造 | 101/110 全过，0.898，11.8s |
| 沙盒 | 同上，10 城子集 36 条 | 硬约束全过率 | 34/36，10.35s |
| 多轮 | `product_revision_cases.json` 30 条 | 整条用例全过率 | 30/30，改 6.0s |
| 意图 | `intent_cases.json` 100 / `holdout` 40 | 分类准确率 | holdout 模型优先 100%，兜底 30% |

```bash
python3 -m evaluation.product_eval --profile api_only --split test --judge-model deepseek-v4-flash
POI_PROVIDER=sandbox python3 -m evaluation.product_eval --profile api_only --split test --skip-judge
python3 -m evaluation.revision_eval --profile api_only
python3 -m evaluation.intent_eval --cases evaluation/intent_holdout.json
python3 -m evaluation.combined --dir results/product_eval
```

每份报告都写 `report.json`：suite、主指标、用例数、数据版本（文件哈希）、评测器版本、逐条检查。**不同 suite 的主指标不可横向比较。**

## 6. 规则手册

`product/constraints/sets/v1.json`，版本 1.3.0，20 条约束，26 个算子。

- 评测调用 `evaluation/product_checks.check_result` → 引擎；
- 运行时 `plancheck` / `crosscheck` 也走同一引擎；
- 数据不足时该条检查**不出现**（`meta={"skip": true}`），不判失败。

## 7. 现在到哪了

**能用**：网页对话、单次规划、多轮修改、撤销/取消、失败重试、沙盒离线评测、四套报告。

**短板**：

- 高德 key 无效 → POI 基本走网页搜索，名字/地址/营业时间不干净；
- 没有天气、票务、营业时间的真实数据（沙盒里有，联网没有）；
- 裁判有 ±0.02 量级方差；CI 的 `DEEPSEEK_API_KEY` 没配，Eval smoke 一直跳过；
- 多轮那 9 项检查还没复用规则手册；
- 沙盒只有 10 城。

## 8. 下一步优先级

1. **数据**：换有效高德 key，或接天气/票务；这是内容质量的天花板。
2. **评测收尾**：多轮检查迁引擎（30 条对照）、CI 配 secret、联合报告进 CI。
3. **内容**：餐饮/住宿槽位已接，缺真实供给与营业时间。

## 9. 从哪读起

1. `docs/PROJECT_MAP.md`（本文）
2. `docs/ARCHITECTURE.md` / `docs/AGENT_DESIGN.md`
3. `product/workflow.py` 的 `AgentWorkflow.run`
4. `product/constraints/sets/v1.json` 与 `docs/CONSTRAINTS.md`
5. `docs/PRODUCT_EVAL.md` / `docs/EVAL_PLAN.md` / `docs/EVAL_SPEC.md`

## 10. 最近里程碑

- 11/13：单轮规划闭环 + 网页对话；
- 11/14–11/20：改某天/顺延/撤销/记忆/失败重试；接 ChinaTravel 沙盒；
- 11/21–11/23：声明式约束层（20 条）替换 if/else；四套评测统一报告格式。
