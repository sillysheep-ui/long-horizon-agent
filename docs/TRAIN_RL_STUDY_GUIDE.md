# train_rl.sh 逐行学习指南

## 一句话定位

这是项目的 **RL 训练脚本**。用 GRPO（Group Relative Policy Optimization）算法，让 SFT 后的模型通过"自行探索 → Judge 打分 → 强化好链路"的方式，提升工具调用的编排能力，是整个项目的最终目标。

---

## 一、整体流程

```
训练数据（rl.jsonl，含用户问题）
    ↓
模型用当前策略走 tool loop（vllm 推理，多轮工具调用）
    ↓
外部 Reward Plugin 给每个轨迹打分（reward_parser_aligned）
    ↓
GRPO 对比同一 prompt 下多条轨迹的相对优势
    ↓
更新模型参数（DeepSpeed ZeRO-3 分布式训练）
    ↓
重复 200 步
```

---

## 逐行详解

### 第 1-2 行：安全模式

```bash
#!/usr/bin/env bash
set -euo pipefail
```

`#!/usr/bin/env bash` — 用环境变量 PATH 里的 bash，跨平台兼容。

`s` 这三个字母的含义前面讲过了：`-e` 出错即停，`-u` 未定义变量报错，`-o pipefail` 管道中任一环节失败整条管道失败。

---

### 第 5-8 行：加载环境变量

```bash
source .env
export JUDGE_API_KEY=${OPENAI_API_KEY}
export JUDGE_BASE_URL=${OPENAI_BASE_URL}
export JUDGE_MODEL=${JUDGE_MODEL_ID}
```

`source .env` — 把 `.env` 里的 `OPENAI_API_KEY="sk-xxx"` 等全部注入当前 shell。

后面三行是把 RL 需要的 Judge 配置提出来，并 `export` 传给子进程。RL 过程中需要调 LLM Judge 给模型生成的答案打分，所以必须有 API key。和 `llm_judge.py` 用的是同一套配置。

---

### 第 11-13 行：NCCL 参数

```bash
export NCCL_IB_DISABLE=1
export NCCL_P2P_DISABLE=0
export NCCL_SOCKET_IFNAME=eth0
```

NCCL 是 NVIDIA 的**多 GPU 通信库**。分布式训练时 6 张 GPU 之间要互传梯度。

| 参数 | 值 | 含义 |
|------|----|------|
| `NCCL_IB_DISABLE=1` | 禁用 InfiniBand（高速网络） | 训练在单机多卡上跑，不需要跨机通信，关了避免找不存在的 IB 设备而报错 |
| `NCCL_P2P_DISABLE=0` | 启用 GPU 间点对点直连 | 同一台机器上的 GPU 通过 NVLink/PCIe 直接通信，不走 CPU 内存中转 |
| `NCCL_SOCKET_IFNAME=eth0` | 指定网络接口 | 告诉 NCCL 用 eth0 网卡通信，防止选错网卡导致连接失败 |

---

### 第 15-16 行：项目路径

```bash
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MS_SWIFT_DIR="${MS_SWIFT_DIR:-${PROJECT_ROOT}/ms-swift}"
```

第一行取脚本所在目录的绝对路径：先用 `dirname` 拿到脚本目录，`cd` 进去，`pwd` 打印绝对路径。比 `pwd` 更可靠——用户可能在任意目录执行 `bash /path/to/train_rl.sh`。

第二行 `${MS_SWIFT_DIR:-默认值}` 的语法含义：如果环境变量 `MS_SWIFT_DIR` 没设或为空，就用默认值。这种 `:-` 写法在脚本里反复出现，让所有路径都可被外部覆盖。

---

### 第 19-20 行：SFT 基座模型和参考模型

```bash
MODEL_PATH="${MODEL_PATH:-${PROJECT_ROOT}/saved/output/qwen3_4b_sft_4gpu/.../qwen3_4b_sft_merged_420}"
REF_MODEL_PATH="${REF_MODEL_PATH:-${MODEL_PATH}}"
```

`MODEL_PATH` — SFT 训练完、LoRA 合并后的模型。GRPO 从这个 checkpoint 开始训。

`REF_MODEL_PATH` — 参考模型（reference model）。GRPO 需要一个固定不训的"基准"来约束策略不要偏离太远（KL 散度惩罚）。默认等于 `MODEL_PATH`，意味着参考模型就是 SFT 模型。

**为什么需要参考模型**：RL 训练中，模型可能为了刷高分学会投机取巧（忽略工具调用直接编答案）。KL 惩罚保证新策略不会离 SFT 模型太远，维持基本的工具调用能力。

---

### 第 23-26 行：数据源和自定义插件

```bash
RL_DATASET="${RL_DATASET:-${PROJECT_ROOT}/data/final/rl.jsonl}"
ANSWER_JUDGE_GOLD_DATASET_PATH="${ANSWER_JUDGE_GOLD_DATASET_PATH:-${PROJECT_ROOT}/data/final/rl.jsonl}"
REWARD_PLUGIN="${REWARD_PLUGIN:-${PROJECT_ROOT}/ms-swift/examples/train/grpo/plugin/tooluse_reward_parser_aligned.py}"
SCHEDULER_PLUGIN="${SCHEDULER_PLUGIN:-${PROJECT_ROOT}/ms-swift/examples/train/grpo/plugin/tooluse_multi_turn_scheduler.py}"
```

| 变量 | 指向 | 作用 |
|------|------|------|
| `RL_DATASET` | `data/final/rl.jsonl` | 训练用的问题集（只有 query，不需要标准答案——RL 靠 reward 信号） |
| `ANSWER_JUDGE_GOLD_DATASET_PATH` | 同上 | Judge 模型打分的"金标准"数据集（含参考答案，用于对比） |
| `REWARD_PLUGIN` | `tooluse_reward_parser_aligned.py` | **自定义 Reward 函数**——整个 RL 训练的大脑。它解析模型生成的 tool loop 轨迹，从多个维度打分 |
| `SCHEDULER_PLUGIN` | `tooluse_multi_turn_scheduler.py` | **多轮调度器**——控制 tool loop 的执行节奏（何时开始、何时强制结束、最大轮次） |

**这两个 plugin 是项目的核心创新点**——不是简单的"答案好不好"打分，而是针对 tool loop 轨迹的结构化评估。

---

### 第 29-32 行：分布式训练配置

```bash
OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_ROOT}/saved/output/grpo_parser_aligned_run}"
TRAIN_CUDA_VISIBLE_DEVICES="${TRAIN_CUDA_VISIBLE_DEVICES:-2,3,4,5,6,7}"
NPROC_PER_NODE="${NPROC_PER_NODE:-6}"
MASTER_PORT="${MASTER_PORT:-29511}"
```

| 变量 | 值 | 含义 |
|------|----|------|
| `OUTPUT_DIR` | 保存 checkpoint 的路径 | 训练过程中的模型快照存在这里 |
| `TRAIN_CUDA_VISIBLE_DEVICES` | `2,3,4,5,6,7` | 用 GPU 2-7 共 6 张卡训练。GPU 0-1 留给 vllm rollout server |
| `NPROC_PER_NODE` | `6` | 6 张卡 = 6 个训练进程（每卡一个） |
| `MASTER_PORT` | `29511` | 分布式训练的主节点通信端口 |

**为什么要分开 GPU**：RL 训练需要同时跑两件事：

- vllm server：用 GPU 0-1 做推理（rollout），生成 tool loop 轨迹
- 训练进程：用 GPU 2-7 做反向传播，更新模型参数

如果不分开，推理和训练会抢显存，OOM。

---

### 第 35-38 行：vllm Rollout 服务配置

```bash
VLLM_SERVER_HOST="${VLLM_SERVER_HOST:-127.0.0.1}"
VLLM_SERVER_PORT="${VLLM_SERVER_PORT:-8000}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.8}"
MAX_TURNS="${MAX_TURNS:-13}"
```

| 变量 | 含义 |
|------|------|
| `VLLM_SERVER_HOST` | vllm 推理服务地址。`127.0.0.1` = 本机 |
| `VLLM_SERVER_PORT` | vllm 服务端口 8000 |
| `VLLM_GPU_MEMORY_UTILIZATION` | vllm 最多用单卡显存的 80%，留 20% 给 KV cache 碎片 |
| `MAX_TURNS` | 单条 rollout 最多 13 轮工具调用——和 `run_tool_loop_infer` 的默认 max_turns 一致 |

---

### 第 41-50 行：训练超参

```bash
MAX_STEPS="${MAX_STEPS:-200}"
SAVE_STEPS="${SAVE_STEPS:-50}"
LEARNING_RATE="${LEARNING_RATE:-1e-6}"
TOOL_LOOP_SYSTEM_MAX_TOOL_CALLS="${TOOL_LOOP_SYSTEM_MAX_TOOL_CALLS:-13}"
PER_DEVICE_TRAIN_BATCH_SIZE="${PER_DEVICE_TRAIN_BATCH_SIZE:-1}"
GRADIENT_ACCUMULATION_STEPS="${GRADIENT_ACCUMULATION_STEPS:-1}"
NUM_GENERATIONS="${NUM_GENERATIONS:-${NPROC_PER_NODE}}"
TEMPERATURE="${TEMPERATURE:-0.9}"
MAX_LENGTH="${MAX_LENGTH:-50000}"
MAX_COMPLETION_LENGTH="${MAX_COMPLETION_LENGTH:-5000}"
```

| 变量 | 值 | 为什么是这个值 |
|------|----|--------------|
| `MAX_STEPS` | 200 | RL 训练步数。每步 = 一轮 rollout + 一次梯度更新。200 步足够模型收敛 |
| `SAVE_STEPS` | 50 | 每 50 步保存一次 checkpoint。200/50 = 4 个快照 |
| `LEARNING_RATE` | `1e-6` | RL 的学习率比 SFT 低 1-2 个数量级。模型已经 SFT 好了，RL 只是微调偏好，步子太大会破坏已有能力 |
| `NUM_GENERATIONS` | = `NPROC_PER_NODE` = 6 | 每个 prompt 生成 6 条轨迹（每条在 1 张卡上独立采样），用于 GRPO 组内比较 |
| `TEMPERATURE` | **0.9** | 比 SFT 推理（0.2）高很多——RL 需要模型"探索"，温度高生成更多样化的工具调用链路。同一 prompt 的 6 条轨迹如果都一样就没有对比意义了 |
| `MAX_LENGTH` | 50000 | 单条样本的最大 token 数（含 prompt + 完整 tool loop + 所有工具返回）。非常大的数字，因为 13 轮工具调用可能产生超长文本 |
| `MAX_COMPLETION_LENGTH` | 5000 | 单次 LLM 调用最多生成 5000 token |

---

### 第 53-59 行：Judge 环境变量

```bash
export ANSWER_JUDGE_GOLD_DATASET_PATH
echo $ANSWER_JUDGE_GOLD_DATASET_PATH
export JUDGE_API_KEY="${JUDGE_API_KEY:-}"
export JUDGE_BASE_URL="${JUDGE_BASE_URL:-}"
export JUDGE_MODEL="${JUDGE_MODEL:-}"
export JUDGE_TIMEOUT_SEC="${JUDGE_TIMEOUT_SEC:-30}"
```

这些环境变量会被 Reward Plugin 里的 Judge 客户端读取——它需要调 LLM API 来给模型生成的答案打分。和 `llm_judge.py` 的机制类似但不完全相同。

---

### 第 61-67 行：课程学习权重配置

```bash
export PARSER_REWARD_TOTAL_STEPS="${PARSER_REWARD_TOTAL_STEPS:-${MAX_STEPS}}"
export PARSER_REWARD_PHASE_RATIOS="${PARSER_REWARD_PHASE_RATIOS:-[0.15, 0.2, 0.65]}"
export PARSER_REWARD_W1="${PARSER_REWARD_W1:-[0.10, 0.10, 0.05, 0.05, 0.10, 0.60]}"
export PARSER_REWARD_W2="${PARSER_REWARD_W2:-[0.05, 0.05, 0.05, 0.05, 0.10, 0.70]}"
export PARSER_REWARD_W3="${PARSER_REWARD_W3:-[0.05, 0.05, 0.05, 0.05, 0.05, 0.75]}"
export PARSER_REWARD_DEBUG="${PARSER_REWARD_DEBUG:-1}"
```

**这是整个 RL 训练最精妙的设计——课程学习（Curriculum Learning）+ 三阶段 Reward 权重演化**：

`PARSER_REWARD_PHASE_RATIOS` 把 200 步训练分成三个比例：`[0.15, 0.2, 0.65]`：

```
Phase 1（前 30 步，15%）：权重 W1 = [0.10, 0.10, 0.05, 0.05, 0.10, 0.60]
Phase 2（第 31-70 步，20%）：权重 W2 = [0.05, 0.05, 0.05, 0.05, 0.10, 0.70]
Phase 3（第 71-200 步，65%）：权重 W3 = [0.05, 0.05, 0.05, 0.05, 0.05, 0.75]
```

Weight 数组对应 6 个维度：

| 索引 | 维度（推测） | Phase 1 权重 | Phase 3 权重 | 趋势 |
|------|------------|------------|------------|------|
| W[0] | 工具调用完整性 | 0.10 | 0.05 | ↓ 早期纠正基本功 |
| W[1] | 轨迹结构规范性 | 0.10 | 0.05 | ↓ 早期纠正基本功 |
| W[2] | 工具选择合理性 | 0.05 | 0.05 | → |
| W[3] | 调用效率（去冗余） | 0.05 | 0.05 | → |
| W[4] | 信息充分性 | 0.10 | 0.05 | ↓ |
| W[5] | **最终答案质量** | **0.60** | **0.75** | ↑ **答案质量越来越重要** |

**设计意图**：

- **Phase 1**：模型刚 SFT 完，可能还会出现格式错误、漏调工具等低级问题。给予工具调用完整性较高的权重（0.10），引导模型先学会"老老实实调工具"。
- **Phase 2**：过渡期，权重平滑调整。
- **Phase 3**：占训练的大头（65%），工具调用的基本功已稳定，核心权重转移到**答案质量**（0.75）。模型开始关注"给出的方案是否完整、准确、可执行"。

这就是课程学习——先教基本功，再提更高要求。

---

### 第 69 行：转到 Swift 目录

```bash
cd "${MS_SWIFT_DIR}"
```

Swift 命令行工具需要在项目根目录运行才能找到配置文件。`ms-swift/` 是阿里魔改的微调框架的本地副本。

---

### 第 72-74 行：启动命令

```bash
CUDA_VISIBLE_DEVICES="${TRAIN_CUDA_VISIBLE_DEVICES}" \
NPROC_PER_NODE="${NPROC_PER_NODE}" \
MASTER_PORT="${MASTER_PORT}" \
swift rlhf \
```

GPU 2-7 上启动 6 进程分布式训练，调用 `swift rlhf`（Swift 框架的 RLHF 训练入口）。

---

### 第 75-80 行：GRPO 核心配置

```bash
  --rlhf_type grpo \
  --model "${MODEL_PATH}" \
  --ref_model "${REF_MODEL_PATH}" \
  --dataset "${RL_DATASET}" \
  --external_plugins "${REWARD_PLUGIN}" "${SCHEDULER_PLUGIN}" \
  --reward_funcs external_parser_aligned_curriculum_reward \
```

| 参数 | 含义 |
|------|------|
| `--rlhf_type grpo` | 使用 GRPO 算法（不需要单独的 Critic 模型，用组内相对比较） |
| `--model` | 要训练的模型（SFT 后的 checkpoint） |
| `--ref_model` | 参考模型（用于 KL 惩罚，阻止策略偏离太远） |
| `--dataset` | RL 训练数据（只含 query，不需要答案） |
| `--external_plugins` | 加载两个自定义插件：reward 函数 + 多轮调度器 |
| `--reward_funcs` | 使用 `external_parser_aligned_curriculum_reward` 这个 reward 函数——就是上面那套带课程学习的打分系统 |

---

### 第 81-86 行：vllm Rollout 配置

```bash
  --use_vllm true \
  --vllm_mode server \
  --vllm_server_host "${VLLM_SERVER_HOST}" \
  --vllm_server_port "${VLLM_SERVER_PORT}" \
  --vllm_gpu_memory_utilization "${VLLM_GPU_MEMORY_UTILIZATION}" \
  --vllm_tensor_parallel_size 2 \
```

| 参数 | 含义 |
|------|------|
| `--use_vllm true` | 用 vllm 做 rollout 推理（高吞吐，比 transformers 快 10-20x） |
| `--vllm_mode server` | vllm 以独立服务运行，训练进程通过 HTTP API 调用。比 `colocate` 模式更稳定 |
| `--vllm_tensor_parallel_size 2` | 2 张 GPU 做张量并行推理。对应 GPU 0-1 |

---

### 第 87-88 行：vllm 优化

```bash
  --vllm_enable_prefix_caching true \
  --vllm_disable_custom_all_reduce true \
```

| 参数 | 含义 |
|------|------|
| `--vllm_enable_prefix_caching true` | 缓存 prompt 前缀的 KV cache。所有样本的 system prompt 完全一样，缓存后只算一次。**RL 训练中最关键的推理加速技巧** |
| `--vllm_disable_custom_all_reduce true` | vllm 的 tensor parallel（TP）模式下，两张 GPU 之间需要同步 KV cache。`custom_all_reduce` 是 vllm 自研的通信 kernel，但有时和训练进程的 NCCL 冲突。禁用后回退到标准 PyTorch 通信，牺牲一点速度换稳定性 |

---

### 第 89-91 行：训练模式

```bash
  --tuner_type full \
  --torch_dtype bfloat16 \
  --deepspeed zero3 \
```

| 参数 | 含义 |
|------|------|
| `--tuner_type full` | **全参数微调**（不是 LoRA）。RL 训练需要更新模型的所有权重来改变行为偏好 |
| `--torch_dtype bfloat16` | 混合精度训练，bfloat16 指数位和 float32 相同，不会溢出 |
| `--deepspeed zero3` | DeepSpeed ZeRO Stage 3——把优化器状态、梯度、模型参数**全部分片**到 6 张卡上。每张卡只存 1/6 的参数。对于 4B 模型的 full-tuning，不开 ZeRO-3 一张卡根本放不下 |

---

### 第 92-98 行：训练步数和保存

```bash
  --learning_rate "${LEARNING_RATE}" \
  --max_steps "${MAX_STEPS}" \
  --save_steps "${SAVE_STEPS}" \
  --save_total_limit 2 \
  --per_device_train_batch_size "${PER_DEVICE_TRAIN_BATCH_SIZE}" \
  --gradient_accumulation_steps "${GRADIENT_ACCUMULATION_STEPS}" \
  --gradient_checkpointing true \
```

| 参数 | 含义 |
|------|------|
| `--learning_rate 1e-6` | 学习率极低 |
| `--max_steps 200` | 总步数 |
| `--save_steps 50` | 每 50 步保存 |
| `--save_total_limit 2` | 只保留最近的 2 个 checkpoint，旧的自动删除。节省磁盘 |
| `--gradient_checkpointing_kwargs '{"use_reentrant": false}'` | PyTorch 新式的 checkpoint 实现，更高效 |

---

### 第 99-104 行：生成长度控制

```bash
  --max_length "${MAX_LENGTH}" \
  --max_completion_length "${MAX_COMPLETION_LENGTH}" \
  --num_generations "${NUM_GENERATIONS}" \
  --temperature "${TEMPERATURE}" \
  --top_k 50 \
  --top_p 0.9 \
```

| 参数 | 含义 |
|------|------|
| `--max_length 50000` | prompt + completion 的总长度上限 |
| `--max_completion_length 5000` | 单次 LLM 调用生成 token 上限 |
| `--num_generations 6` | 每个 prompt 生成 6 条轨迹 |
| `--temperature 0.9` | 高温度鼓励探索 |
| `--top_k 50` | 只从概率前 50 的 token 采样 |
| `--top_p 0.9` | nucleus sampling 累积阈值 |

---

### 第 105-106 行：GRPO 特有参数

```bash
  --beta 0.04 \
  --loss_type grpo \
```

| 参数 | 含义 |
|------|------|
| `--beta 0.04` | KL 散度惩罚系数。值越大，策略越不能偏离参考模型。0.04 是较小值——允许一定程度的探索 |
| `--loss_type grpo` | 使用 GRPO 损失函数（组内相对优势 + 重要性采样 + KL 惩罚的三合一） |

---

### 第 107-112 行：多轮调度与奖励

```bash
  --multi_turn_scheduler travel_tool_loop \
  --max_turns "${MAX_TURNS}" \
  --completion_length_limit_scope per_round \
  --scale_rewards group \
  --importance_sampling_level token \
  --num_iterations 1 \
```

| 参数 | 含义 |
|------|------|
| `--multi_turn_scheduler travel_tool_loop` | 使用自定义的多轮调度器 `tooluse_multi_turn_scheduler.py`。它知道 travel Agent 的 tool loop 应该在什么条件下结束 |
| `--max_turns 13` | 每条轨迹最多 13 轮工具调用 |
| `--completion_length_limit_scope per_round` | token 长度限制**按轮次计算**（不是整条轨迹）。让模型每轮最多生成 5000 token，但整条轨迹可以很长 |
| `--scale_rewards group` | Reward 归一化：同组 6 条轨迹的 reward 减均值除以标准差。让 reward 信号变成"相对优势"而不是绝对值。**GRPO 的核心——不需要 Critic 模型估计 baseline，直接组内比较** |
| `--importance_sampling_level token` | 重要性采样到 token 级别。因为 tool loop 的每步都是模型自己在上一轮输出后生成的，需要用 token 级重要性采样修正分布偏移 |
| `--num_iterations 1` | 每批数据只用一次就丢。RL 中数据 reuse 过多会导致过拟合 |

---

### 第 113-117 行：日志

```bash
  --dataloader_num_workers 1 \
  --dataset_num_proc 1 \
  --logging_steps 1 \
  --log_completions true \
  --report_to tensorboard \
  --output_dir "${OUTPUT_DIR}"
```

| 参数 | 含义 |
|------|------|
| `--dataloader_num_workers 1` | 用 1 个进程加载数据。数据已经很小了，多了浪费 |
| `--dataset_num_proc 1` | 数据预处理也用 1 进程 |
| `--logging_steps 1` | **每步都打日志**。默认可能 10 步打一次，RL 训练步数才 200，每步都值得记录 |
| `--log_completions true` | 把模型每步生成的完整轨迹（含 tool loop）写入日志。用于训练后分析模型行为变化 |
| `--report_to tensorboard` | 把 loss、reward 等指标的曲线写到 TensorBoard。`tensorboard --logdir=...` 可以在浏览器里实时看训练进度 |
| `--output_dir` | 所有 checkpoint 和日志都输出到这个目录 |

---

## 二、GRPO 在这个场景下的工作方式

```
Step 1:
  从 rl.jsonl 取 1 个 query "帮我规划北京三日游"

  模型用当前策略 + temperature 0.9，独立采样 6 次 →
    轨迹 A: weather → hotel → route → answer "方案1..."  (reward = 7.2)
    轨迹 B: hotel → search → answer "方案2..."           (reward = 5.8)
    轨迹 C: weather → hotel → catering → route → answer "方案3..." (reward = 8.1)
    轨迹 D: 直接 answer "方案4..."                       (reward = 3.0)
    轨迹 E: search → visit → route → answer "方案5..."  (reward = 6.5)
    轨迹 F: weather → poi → hotel → route → answer "方案6..." (reward = 7.8)

  scale_rewards("group"):
    均值 avg ≈ 6.4, 标准差 std ≈ 1.72
    归一化后: A=-0.14, B=-0.76, C=0.99, D=-1.97, E=0.06, F=0.81

  轨迹 C 获得最高正奖励 → 模型学习 C 的工具调用模式
  轨迹 D（不调工具直接答）获得负奖励 → 模型抑制这种行为

Step 2:
  下一个 query → 模型已经稍微更倾向于"先查天气再找酒店和餐厅再规划路线"
  继续采样 → 继续对比 → 继续优化...
```

**注意**：因为 `scale_rewards=group`，模型永远和自己比。同一 query 下的 6 条轨迹相互竞争，即使所有轨迹的绝对质量都很低，最好的那个也会被强化。这使得 GRPO 在不需要 Critic 模型的情况下就能稳定训练。

---

## 三、三阶段课程学习的可视化

```
Phase 1 (0-30步)          Phase 2 (31-70步)        Phase 3 (71-200步)
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│ 工具完整性 0.10 │    │                 │    │                 │
│ 格式规范   0.10 │    │  权重平滑过渡   │    │ 答案质量   0.75 │
│ 工具选择   0.05 │    │                 │    │                 │
│ 效率      0.05 │    │                 │    │ 其他维度   0.05 │
│ 信息充分  0.10 │    │                 │    │                 │
│ 答案质量   0.60 │    │                 │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘

重点：学会"查"           过渡期                  重点：学会"答"
```

---

## 四、与 SFT 训练的关键区别

| | SFT (`train_sft.sh`) | RL (`train_rl.sh`) |
|---|---|---|
| **目标** | 模仿蒸馏数据的 tool loop | 探索更好的 tool loop |
| **数据** | 完整对话（含标准答案和工具轨迹） | 只有 query（靠 reward 信号） |
| **优化** | 交叉熵损失（逐 token 模仿） | GRPO 损失（相对优势 + KL） |
| **学习方法** | "这句话应该这样说" | "这样做比那样做好" |
| **模型** | 基座模型（pretrained） | SFT 后的模型 |
| **学习率** | 较高 | 很低（1e-6） |
| **温度** | 推理时 0.6 | 探索时 0.9 |
| **奖励来源** | 无（直接用正确答案） | Reward Plugin + Judge LLM |
| **多轮** | 数据里已经写好了 | 调度器在 rollout 中动态执行 |
