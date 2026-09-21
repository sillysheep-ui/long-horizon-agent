# Tool DAG 使用指南

## 功能定位

Tool DAG 将旅行任务先编译成结构化工具依赖图，再由确定性调度器并发执行。它与原有 ReAct 推理共存：DAG 规划或执行完全失败时，默认自动回退到 `tool_loop`。

```
用户问题 -> Planner(JSON DAG) -> 并发工具调度 -> 证据汇总 -> 最终回答
                                   |
                                   +-- 失败时回退 ReAct
```

## 核心文件

- `tool_dag.py`：数据模型、环检测、参数引用、异步调度、重试和缓存。
- `run_tool_dag_infer.py`：模型规划、工具执行、答案生成和 ReAct 回退入口。
- `tests/test_tool_dag.py`：不依赖模型和外部 API 的调度器单元测试。

## DAG 格式

```json
{
  "goal": "规划北京三日游",
  "nodes": [
    {
      "id": "weather",
      "tool": "weather_search",
      "arguments": {"city": "北京"},
      "depends_on": [],
      "timeout_seconds": 15,
      "retries": 1
    },
    {
      "id": "nearby",
      "tool": "around_search",
      "arguments": {
        "location": "116.397,39.908",
        "radius": 5000,
        "keyword": "酒店"
      },
      "depends_on": []
    }
  ]
}
```

同一层中没有依赖关系的节点会受 `--dag_max_concurrency` 控制并发执行。

下游节点可用 `{{node_id}}` 引用完整上游输出；如果上游输出为 JSON，可用 `{{node_id.field.path}}` 提取字段。凡是在参数中引用的节点，必须显式写入 `depends_on`。

## 运行

Transformers 后端：

```bash
python run_tool_dag_infer.py \
  --model_dir saved/output/qwen3_4b_sft_merged_420/ \
  --query "帮我规划北京三日游，预算3000元" \
  --tools_dir tools/ \
  --dag_max_concurrency 4
```

vLLM 后端：

```bash
python run_tool_dag_infer.py \
  --infer_backend vllm \
  --model_dir saved/output/qwen3_4b_sft_merged_420/ \
  --query "帮我规划北京三日游，预算3000元" \
  --tools_dir tools/
```

读取数据集：

```bash
python run_tool_dag_infer.py \
  --model_dir saved/output/qwen3_4b_sft_merged_420/ \
  --dataset_path data/final/test_final.jsonl \
  --sample_idx 0
```

默认开启 `--react_fallback`。调试 DAG 时可传入 `--no-react_fallback`，让规划或执行错误直接暴露。

## 输出指标

结果 JSON 会保存：

- `levels`：调度器实际执行的并发层级；
- `latency_ms`：DAG 工具阶段总耗时；
- `success_count/failed_count/skipped_count`：节点执行状态；
- 每个节点的 `latency_ms`、`attempts` 和 `cache_hit`；
- Planner 生成的完整结构化计划与最终回答。

这些字段可以直接用于对比串行 ReAct 与 Tool DAG 的 P50/P95 延迟、平均调用数、失败率和并发收益。

## 测试

```bash
python -m unittest tests.test_tool_dag -v
```

## 零样本基线测评

先用现有 GRPO checkpoint 直接测试 DAG 能力，不进行任何新训练：

```bash
python benchmark_tool_dag.py \
  --dataset_path data/final/test_final.jsonl \
  --model_dir saved/output/grpo_parser_aligned_run/v8-20260514-140934/checkpoint-150/ \
  --tools_dir tools/ \
  --modes react dag \
  --start_idx 0 \
  --num_samples 80 \
  --output_dir test_output/tool_dag_baseline
```

建议第一次只跑 5 条完成冒烟测试，再扩大到全部 80 条：

```bash
python benchmark_tool_dag.py \
  --dataset_path data/final/test_final.jsonl \
  --model_dir saved/output/grpo_parser_aligned_run/v8-20260514-140934/checkpoint-150/ \
  --tools_dir tools/ \
  --num_samples 5
```

输出目录包含：

- `react_records.jsonl`：原始 ReAct 逐样本结果与完整消息；
- `dag_records.jsonl`：DAG、节点耗时、并发层级与最终回答；
- `summary.json`：机器可读的聚合指标；
- `report.md`：ReAct 与 Tool DAG 对比表。

为保证比较公平，两种模式应使用同一个模型 checkpoint、测试集、采样参数和工具环境。系统成功率与延迟不能代表答案质量，最终报告还应使用现有双向 LLM Judge 比较两组 `messages`。
