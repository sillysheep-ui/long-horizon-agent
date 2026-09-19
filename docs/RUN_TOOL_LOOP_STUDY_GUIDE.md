# run_tool_loop_infer.py 学习指南

## 一句话定位

这是整个项目的**核心推理引擎**。它把 LLM 和 12 个工具串联起来，让模型边想边调工具，直到产出最终答案。

---

## 一、整体流程图

```
┌──────────────────────────────────────────────────────┐
│                   run_single()                        │
│                                                      │
│  加载数据 → 初始化 messages → 进入 tool loop           │
│                                                      │
│   for turn in 1..max_turns:                          │
│     ┌──────────────────────────────────────┐         │
│     │  ① LLM 推理 → 输出文本                │         │
│     │  ② 检查是否包含 <answer>？            │         │
│     │     是 → 提取答案，结束               │         │
│     │     否 → 继续                         │         │
│     │  ③ 解析 <tool_call>                  │         │
│     │     解析失败/缺失 → 提示模型重试      │         │
│     │     解析成功 → 执行工具              │         │
│     │  ④ 工具结果回填到 messages           │         │
│     │  ⑤ 安全检测（重复调用/无效循环）      │         │
│     │  ⑥ 强制收束（超过N轮强制输出answer）  │         │
│     └──────────────────────────────────────┘         │
│                                                      │
│  返回 { status, prediction, tool_calls, ... }        │
└──────────────────────────────────────────────────────┘
```

---

## 二、最难理解的核心概念：messages 的演变

这是理解这个文件最关键的一点。`messages` 不是静态的，它随着 tool loop 进行不断**增长**：

```
第 0 轮（初始）:
  [system]   ← 系统提示词
  [user]     ← 用户问题 "帮我规划北京三日游"

第 1 轮（LLM 想搜酒店）:
  [system]
  [user]
  [assistant]  ← "<tool_call>{\"name\":\"hotel_search\",\"arguments\":{...}}</tool_call>"
  [tool]       ← "## 1\nname: 北京希尔顿..."（工具返回结果）

第 2 轮（LLM 想搜天气）:
  [system]
  [user]
  [assistant]   ← 第1轮的 tool_call
  [tool]        ← 第1轮的结果
  [assistant]   ← "<tool_call>{\"name\":\"weather_search\",\"arguments\":{...}}</tool_call>"
  [tool]        ← "## 天气预报\n..."（天气结果）

第 3 轮（信息够了，给答案）:
  [system]
  [user]
  [assistant]   ← 第1轮的
  [tool]        ← 第1轮的
  [assistant]   ← 第2轮的
  [tool]        ← 第2轮的
  [assistant]   ← "<answer>您的北京三日游方案如下...</answer>"
  → 检测到 <answer>，结束
```

每一轮 LLM 都能"看到"之前所有的对话历史和工具返回结果，所以它知道已经查了什么、还缺什么。这就是 Agent 能**多步决策**的根基。

---

## 三、逐函数详解（按重要性排序）

### 3.1 `run_single()` — 主循环（第 372 行）

**这是整个文件的心脏。**

```python
async def run_single(args, tokenizer, model, llm, sample_idx: int) -> Dict[str, Any]:
```

返回值是一个字典，包含 `status`（结束原因）、`prediction`（最终答案）、`tool_calls`（调了多少次工具）、`turns`（经历了多少轮）。

**8 个防护变量**（第 399-410 行）：

| 变量 | 初始值 | 作用 |
|------|--------|------|
| `no_tool_no_answer_retries` | 0 | 模型既不调工具也不给答案，重试计数 |
| `total_tool_calls` | 0 | 总共调了几次工具 |
| `saw_tool_response` | False | 是否收到过工具结果（强制执行 tool_first） |
| `consecutive_invalid_tool_rounds` | 0 | 连续调用无效工具的轮次 |
| `previous_tool_call_signature` | "" | 上一轮的工具调用签名（检测重复） |
| `previous_tool_response_signature` | "" | 上一轮的结果签名（检测重复） |
| `consecutive_same_tool_call_rounds` | 0 | 连续完全相同的工具调用轮次 |
| `repeated_loop_final_answer_chance_used` | False | 死循环时是否给过"最后一次机会" |

**主循环骨架**（第 411 行起）：

```python
for turn in range(1, args.max_turns + 1):
    # ① 推理
    resp = infer_once(...)
    messages.append({"role": "assistant", "content": resp})

    # ② tool_first 检查
    if 有<answer> 且 没收到过工具结果 且 开启了tool_first:
        注入提示"请先调工具" → continue

    # ③ 检测 <answer> 并提取
    if 有<answer> 且 (没开tool_first或已收到工具结果):
        提取答案 → break

    # ④ 解析 tool_call
    tool_calls = parse_tool_calls(resp)
    if 没解析出且有不完整的tool_call:
        提示"补全" → continue
    if 没解析出也没tool_call:
        提示"请调工具或给答案" → 重试/break

    # ⑤ 逐个执行工具
    for call in tool_calls:
        提取函数名和参数
        加载工具 → 调用 → 截断长结果
        messages.append({"role": "tool", ...})

    # ⑥ 安全检测
    检查无效循环 / 重复调用 / 强制收束
```

#### 关键难点：为什么 `continue` 而不是 `break`？

第 423-432、444-452、455-476 行的 `continue` 是**防护式重试**。当模型输出不符合预期时，不会直接终止，而是往 messages 里注入一条纠正提示，让模型下一轮重新输出。

例如 tool_first 检查（第 423 行）：
```
用户问了问题 → LLM 偷懒直接 <answer> → 系统注入 "请先通过 tool_call 调用工具"
→ LLM 看到这条消息，下一轮开始调工具
```

这种"注入纠正 + continue"的模式在整个循环中反复出现。

---

### 3.2 `parse_tool_calls()` — 工具调用解析器（第 157 行）

**这是整个项目最复杂的解析函数，也是最容易出 bug 的地方。** 因为模型输出的格式千奇百怪。

核心流程：

```
模型输出文本
    ↓
① 正则提取 <tool_call>...</tool_call> 块
    ↓ 成功
② JSON 解析（先 json.loads，失败用 json_repair.loads）
    ↓
③ _normalize_calls() 归一化 → 统一成 [{"name": "xxx", "arguments": {...}}]
```

**`_normalize_calls()` 处理了 6 种格式变体**：

| 情况 | 输入示例 | 处理策略 |
|------|---------|---------|
| OpenAI 标准 | `{"tool_calls": [{"function": {"name": "search", "arguments": {...}}}]}` | 从 `function` 嵌套对象里取 |
| 扁平命名 | `{"tool": "search", "parameters": {...}}` | 从 `tool`/`parameters` 取 |
| 另一种扁平 | `{"tool_name": "search", "tool_input": {...}}` | 从 `tool_name`/`tool_input` 取 |
| 单键映射 | `{"search": {...}}` | 如果 key 在 TOOL_CLASS_MAP 中，当作工具名 |
| 单对象 | `{"name": "search", "arguments": {...}}` | 直接使用 |
| 数组 | `[{...}, {...}]` | 逐个调用 `extract_call()` |

**`<tool_calls>` 标签兼容**（第 206-213 行）：有些模型不输出 `<tool_call>` 而是输出 `<tool_calls>`（多一个 s），代码也处理了。

**三次容错**：
1. `json.loads` — 标准解析
2. `json_repair.loads` — 修复后解析（处理缺引号、多余逗号等）
3. 回退匹配 `{...}` 模式（第 199 行）— 最后的救命稻草

---

### 3.3 `extract_call()` — 从单个调用对象提取函数名和参数（第 221 行）

这个函数的必要性：不同模型产出的调用对象结构不一致。

```python
def extract_call(call: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
```

处理了 5 种参数位置：
```python
args = (
    function_obj.get("arguments")   # OpenAI 标准：function.arguments
    or function_obj.get("parameters")  # 变体：function.parameters
    or call.get("arguments")        # 顶层 arguments
    or call.get("parameters")       # 顶层 parameters
    or call.get("input")            # 顶层 input
    or {}
)
```

如果参数是 JSON 字符串，自动 `json.loads` 转成字典。如果解析失败，用 `{"raw_arguments": 原始字符串}` 兜底，至少不会丢失数据。

---

### 3.4 `load_tool()` — 动态工具加载（第 144 行）

```python
def load_tool(tools_dir: str, tool_name: str):
```

使用 `importlib` 动态加载模块（不是静态 import），因为：
1. 12 个工具分布在 12 个不同的 .py 文件中
2. 运行时才知道 LLM 要调用哪个工具
3. 避免把所有 12 个工具都 import 到内存

```python
spec = importlib.util.spec_from_file_location("tool_module", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cls = getattr(module, cls_name)
return cls()  # 实例化
```

注意 `TOOL_CLASS_MAP` 中每个工具名映射到 `(文件名, 类名)` 元组。`module_path` 是 `tools_dir + 文件名`。

---

### 3.5 `canonical_tool_call_signature()` — 工具调用签名（第 248 行）

```python
def canonical_tool_call_signature(tool_calls: List[Dict[str, Any]]) -> str:
    normalized = [{"name": fn or "", "arguments": args or {}} for call in tool_calls]
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)
```

把所有工具调用归一化为统一文本，用于**重复检测**。`sort_keys=True` 确保即使模型输出时字段顺序不同，相同参数也会被识别为同一调用。

---

### 3.6 `_simplify_system_prompt()` — 系统提示词简化（第 56 行）

原始的 system prompt 可能很长（包含所有 12 个工具的完整 JSON Schema）。对于已经训练过的模型，这些冗余信息会影响推理效率。这个函数把 system prompt 压缩为精简版：

```python
concise_rules = [
    "你是旅行规划助手，需要先用工具获取事实，再给最终回答。",
    "工具阶段：<tool_call>{\"name\": ..., \"arguments\": ...}</tool_call>",
    "最终阶段：<answer>...</answer>",
    "# HARD LIMIT（必须遵守的硬限制）"
]
```

通过 `TOOL_LOOP_SIMPLIFY_SYSTEM_PROMPT` 环境变量控制是否启用。默认开启。

---

### 3.7 安全防护机制（最多人忽略，最重要）

| 防护 | 控制参数 | 触发后行为 |
|------|---------|-----------|
| **tool_first** | `--tool-first-enforce` (默认开启) | LLM 没调工具就想给答案 → 注入提示重试 |
| **partial tool_call** | 自动检测 | `<tool_call>` 开标签没关 → 提示补全 |
| **no tool no answer** | `--max-no-tool-no-answer-retries` (默认2) | 既不调工具也不给答案 → 提示后重试，超限后 break |
| **invalid tool loop** | `--max-invalid-tool-rounds` (默认3) | 连续调用了无效工具 → 超限后 skip sample |
| **repeated call loop** | `--max-same-tool-call-rounds` (默认3) | 连续完全相同调用+相同结果 → 先给最后一次强制 answer 机会，再失败就 skip |
| **force answer** | `--force-answer-after-turns` (默认12) | 超过 N 轮还没出答案 → 注入"请直接给答案" |
| **max turns** | `--max-turns` (默认20) | 总轮次上限 → 超限标记为 max_turns |
| **response truncation** | `--tool-response-max-chars` (默认5000) | 单个工具返回结果截断 |

**"重复调用检测" 的设计精妙之处**（第 564-600 行）：

普通实现可能只比较 tool_call 是否相同，但这个项目同时比较 **tool_call 签名 + 结果签名**：

```python
if (call_signature == previous_call_signature      # 调用相同
    and result_signature == previous_result_signature):  # 结果也相同
    触发重复检测
else:
    reset 计数  # 调了不同工具，或同一工具但结果不同，都不算重复
```

这意味着：用户搜 `"火锅"` 后搜 `"川菜"`，虽然都调 `catering_search`，但参数不同，结果不同，不算重复。只有**真正的死循环**才会被拦截。

---

### 3.8 `retry_route_with_poi_if_needed()` — 智能重试（第 267 行）

当 LLM 调用 `route_planning` 时传了地名（"天安门"）而非经纬度，高德 API 会返回 `INVALID_PARAMS`。这个函数自动兜底：

```
route_planning("天安门", "颐和园")  → INVALID_PARAMS
    ↓ 自动触发
poi_search("天安门") → 116.397,39.908
poi_search("颐和园") → 116.268,39.992
    ↓
route_planning("116.397,39.908", "116.268,39.992") → 成功！
```

第 514-522 行的调用点值得注意：只有当 `tool_result` 中包含 `INVALID_PARAMS` 才会触发，不会对所有路线规划都多此一举。

---

### 3.9 两个推理后端

```python
# transformers 后端（第 313 行）— CPU/GPU 直接推理
def infer_once(model, tokenizer, messages, args):
    prompt = tokenizer.apply_chat_template(messages, ...)  # 把 messages 拼成 chat template
    enc = tokenizer(prompt, return_tensors="pt")
    with torch.no_grad():
        out = model.generate(**enc, ...)
    return tokenizer.decode(...)  # 只拿新生成的部分（不要 prompt 本身）

# vllm 后端（第 329 行）— 高性能推理引擎
def infer_once_vllm(llm, tokenizer, messages, args):
    prompt = tokenizer.apply_chat_template(messages, ...)
    outputs = llm.generate([prompt], sampling_params=...)
    return outputs[0].outputs[0].text
```

关键细节：`out[0][enc["input_ids"].shape[-1]:]` 这一步是**只取新生成 token**，因为 model.generate 返回的是 prompt + 新 token。sliding 掉 prompt 部分，只保留新内容。

---

### 3.10 `normalize_system_prompt_inplace()` — 系统提示词标准化（第 113 行）

做两件事：
1. 把 system prompt 里的 `最大可调用N轮工具` 替换为实际配置值
2. 如果环境变量允许，把 system prompt 压缩为精简版（调用 `_simplify_system_prompt`）

---

## 四、5 种结束状态（status）

| status | 含义 | 触发条件 |
|--------|------|---------|
| `answer` | ✅ 正常结束 | 模型输出了 `<answer>` |
| `max_turns` | ⚠️ 轮次耗尽 | 达到 max_turns 还没出 answer |
| `no_tool_no_answer` | ❌ 模型卡住 | 既不调工具也不给答案，超过重试次数 |
| `invalid_tool_call_loop` | ❌ 连续无效 | 连续调了无效工具超过限制 |
| `repeated_tool_call_loop` | ❌ 死循环 | 连续相同调用+相同结果超过限制 |

---

## 五、关键数据流：一条完整链路举例

```
用户 query: "北京三日游，预算3000"

turn=1: LLM → "先看看天气和酒店"
  <tool_call>{"name":"weather_search","arguments":{"city":"北京"}}</tool_call>
  <tool_call>{"name":"hotel_search","arguments":{"location":"116.40,39.90","radius":5000}}</tool_call>
  → 两个工具并行返回结果 → messages 增长 3 条

turn=2: LLM → "搜下景点和交通"
  <tool_call>{"name":"search","arguments":{"query":["北京必去景点","北京三日游攻略"]}}</tool_call>
  → 搜索结果返回 → messages 增长 2 条

turn=3: LLM → "信息够了"
  <answer>
  第一天：天安门 → 故宫 → 王府井，晚上住...
  第二天：八达岭长城 → 鸟巢水立方，预算...
  第三天：颐和园 → 圆明园 → 返程
  总预算：2800元
  </answer>
  → 检测到 <answer> → break → 返回结果
```

---

## 六、阅读建议

**按这个顺序读代码**：

1. 先看 `main`（第 631 行）— 理解命令行参数有哪些
2. 再看 `run_single`（第 372 行）— 理解主循环骨架（只看 for 循环的结构，跳过细节）
3. 细看 `parse_tool_calls`（第 157 行）— 理解模型输出怎么被解析
4. 看 `extract_call`（第 221 行）— 理解归一化逻辑
5. 回头看 `run_single` 的安全防护块（第 549-608 行）— 理解各种 edge case 怎么兜底

**可以暂时跳过的**：
- `infer_once` / `init_backend` — 都是标准的 transformers/vllm 调用，没有项目特殊逻辑
- `_simplify_system_prompt` — 不影响理解核心流程
- `load_jsonl_row` / `load_system_prompt_from_dataset` — 纯数据加载
