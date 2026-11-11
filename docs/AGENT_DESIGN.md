# 产品 Agent 设计（v1）

本文定义产品层 Agent 的职责边界、数据契约和演进路线，用于替代当前 `product/orchestrator.py` 里“三段提示词 + 隐式兜底”的做法。

## 1. 设计原则

1. **事实来自工具，模型只做决策与表达。** 地点、天气、票务、路线必须来自工具或网页证据；模型负责拆解、筛选、组织语言。
2. **模型输出一律过校验。** JSON 解析、字段类型、证据引用、标题合法性都在代码层检查，不依赖模型自觉。
3. **失败必须显式。** 降级、缺项、未核实都要写进结果和页面，禁止静默换供应商或静默丢内容。
4. **确定性层可测试。** 调度、校验、清洗、补全不依赖模型，能用单测覆盖。
5. **协议与模型解耦。** 同一套编排能接两种模型协议（结构化 JSON / 训练时的 ReAct），换模型不改流程。

## 2. 总体结构

```
用户请求
  -> API（job 化，可轮询）
  -> Orchestrator（状态机）
       Intake -> Planner -> Scheduler -> Verifier -> Answerer -> Postcheck
  -> Store（SQLite）+ Events（进度）+ Web（按天展示）
```

对应现有代码：

| 环节 | 现状 | 目标 |
|---|---|---|
| Intake | `orchestrator.py` 提示词抽 JSON | 保留，补 missing_required 校验 |
| Planner | 提示词产出 `poi_queries` | 产出 `ToolPlan`（复用 `inference/tool_dag.py`） |
| Scheduler | 手写 for 循环调工具 | 复用 `ToolDAGScheduler`（并发/依赖/重试/缓存） |
| Verifier | 只做 evidence 状态归类 | 覆盖度、上下文、时长价格矛盾检查 |
| Answerer | 提示词产 JSON | 保留，加 schema 校验与确定性回退 |
| Postcheck | 无 | 标题清洗、条数补全、来源标注 |

## 3. 组件契约

### 3.1 Intake

输出：

```json
{"departure_city":"", "destinations":[], "days":0, "budget":"", "preferences":[], "missing_required":[]}
```

- 未提到的字段留空，**不猜城市、不猜天数**。
- `days` 缺失时不再默认 1 天，改为 `missing_required` 标注并让 Planner 按“一日游”降级展示。

### 3.2 Planner

输出 `ToolPlan`，复用 `inference/tool_dag.py` 的 `ToolPlan.from_dict()` 与 `validate()`：

```json
{"goal":"...", "nodes":[{"id":"d1-p1","tool":"web_search","arguments":{"query":["杭州 西湖 自然风光"]},"depends_on":[]}]}
```

约束：

- 工具必须在白名单内（`web_search`/`poi_search`/`weather_search`/`route_planning`/…）。
- 节点数 ≤ 8（`fit_max_nodes`），禁止环（`validate` 已做）。
- 每日 2–3 个查询；查询词必须包含城市名。
- 规划失败时用确定性模板兜底（每天一个“城市+偏好”查询），不直接失败。

### 3.3 Scheduler

直接复用 `ToolDAGScheduler`：

- 依赖感知并发、单节点超时与重试、结果缓存。
- 每个节点产出统一 `Evidence`（`product/provenance.py` 的 `build_evidence`）：
  - `status`: `ok` / `empty` / `failed` / `unavailable`
  - `source_kind`: `map_poi` / `web_search` / `simulated` / `none`
  - `verification_level`: `address_verified` / `source_linked` / `unverified` / `none`
  - `fetched_at`、`reused`、`reason`

### 3.4 Verifier

规则优先，模型只做解释性补充。

检查项：

1. **覆盖度**：每天至少 1 条 `ok` 证据；不足则触发定向补查（最多 2 轮）。
2. **上下文一致**：证据城市与当天城市一致；跨城交通证据必须覆盖出发地/目的地。
3. **数值矛盾**：时长、价格、营业时间若在证据间冲突，标记 `conflict` 并要求 Answerer 写“以官方为准”。
4. **模拟数据**：`source_kind=simulated` 必须出现在 `notes` 且页面加“模拟”标签。

### 3.5 Answerer

输出：

```json
{"summary":"...", "days":[{"day":1,"city":"...","activities":[{"title":"...","description":"...","evidence_id":"d1-p1"}]}], "notes":["未核实事项"]}
```

硬约束（代码校验，不通过则走确定性回退）：

- `title` 必须能匹配某条 `ok` 证据的 `items[].name`（规范化后全等或包含）。
- `evidence_id` 必须存在且状态为 `ok`。
- 每条活动必须有 `description`，且不得出现证据里没有的具体价格/班次/时间。

### 3.6 Postcheck（新增，确定性）

1. **标题清洗**：去掉 `【】`、`#`、长尾逗号片段；超长截断到 30 字；清洗后再做一次匹配。
2. **条数补全**：每天活动 < 2 时，从未使用的 `ok` 证据里按顺序补，标 `source="auto_fill"`。
3. **来源行**：每条活动展示 `来源 / 查询时间 / 核实级别 / 是否模拟`。
4. **未核实**：`notes` 里聚合所有“未提供营业时间/门票/路线”的项。

## 4. 降级与错误

| 环节失败 | 行为 | 用户可见 |
|---|---|---|
| Intake 缺字段 | 继续，缺失进 `missing_required` | 页面提示“未提供天数，按一日游展示” |
| Planner 失败 | 模板兜底 | `degraded.reason="planner_fallback"` |
| 工具失败 | 记 `failed`，跳过该节点 | 证据卡“不可用”+ 原因 |
| 全部证据失败 | `status=partial` | 明确“无法给出可信具体行程” |
| Answerer 解析失败 | 确定性拼装（证据列表直接成行程） | `degraded.reason="answer_fallback"` |
| 模型超时 | 换下一个 provider | `degraded.provider_changed=true`，页面标注 |

结果里统一增加：

```json
{"degraded":{"used":true,"reason":"answer_fallback","provider_changed":false}}
```

## 5. 状态与可观测性

- job 状态：`queued` / `running` / `partial` / `complete` / `failed`。
- 事件：每个阶段一条，含耗时、工具名、证据数。
- `model_calls`：role、provider、model、latency、prompt/completion tokens（当前缺失，需补）。
- 运行产物落 `product_data/`（SQLite + 可选 JSON trace），为后续 SFT 采集做准备。

## 6. 与训练协议的关系

模型训练时用的是 ReAct：`<tool_call>{...}</tool_call>` 与 `<answer>...</answer>`（见 `prompts/prompt.py`）。本架构默认用 DAG Planner，把“选工具”交给模型、“执行工具”交给调度器。

两层适配：

| 协议 | 适用 | 做法 |
|---|---|---|
| `json_contract` | 通用模型（DeepSeek）、本地 4B | Planner/Answerer 各一次调用，DAG 由 Planner 输出 JSON |
| `tool_loop` | 训练过的 4B，实验用 | 复用 `inference/run_tool_loop_infer.py` 的协议，模型自主多轮调工具，`<answer>` 解析后进同一套校验 |

选择开关：`MODEL_PROTOCOL=json_contract|tool_loop`，默认 `json_contract`。

## 7. 评测

**确定性层**（pytest，不需模型）：

- Planner 输出非法 JSON / 未知工具 / 成环 → 被拒绝并兜底。
- 标题清洗：`【携程美食林】杭州云径天竹咖啡馆…` → `杭州云径天竹咖啡馆`。
- 条数补全：证据 3 条、模型只给 1 条 → 补到 3 条。
- 降级标记：模拟 provider 超时 → `degraded.used=true` 且 `provider_changed=true`。

**场景层**（golden，10–20 条固定请求）：

- 每天活动 ≥ 2，且全部挂 `ok` 证据。
- 无来源的价格/班次/营业时间出现次数为 0。
- 缺天数、缺预算、工具全失败三种极端场景不崩。

**指标**：证据覆盖率、活动数、标题合法率、未核实标注率、事实冲突数、端到端耗时、token 成本。

## 8. 落地顺序

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | Postcheck（清洗/补全）+ 显式降级 + token 记录 | 单测通过，页面出现来源与降级标记 |
| P1 | Planner 改出 `ToolPlan`，Scheduler 复用 `ToolDAGScheduler` | 同一请求证据数不变，耗时下降 |
| P2 | Verifier 覆盖度检查 + 定向补查 | 工具失败时仍保留部分行程 |
| P3 | 多轮修改（换某天、换某活动）+ 版本守卫测试 | 旧结果不覆盖新版本 |
| P4 | golden 场景 + trace 采集 | 指标可对比，数据可导出 |

## 9. 待定

1. 是否保留 `tool_loop` 模式（训练协议）作为可选项。
2. 多轮修改是否走“补丁计划”而非全量重跑。
3. 本地 4B 与云端的路由策略（当前 `MODEL_PROFILE`，是否按阶段分别配置）。
