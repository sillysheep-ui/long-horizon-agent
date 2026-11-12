# Pi Agent 可行性验证（P0 spike）

验证目标：Pi（`pi-python-core`）能否作为产品侧会话外壳，接住真实模型、本地 Qwen3-4B、工具调用、会话恢复与中途干预。

- 日期：2026-11-13
- 环境：macOS / Python 3.13.9 / `pi-python-core==0.12.0` / MLX 本地服务 8080
- 代码只存在于 `/tmp/pi-spike`，未改动产品代码

## 结论

可行。Pi 只做会话外壳：维护多轮消息、执行工具、支持中途 steer/abort；规划仍走现有 Planner → Tool DAG → Verifier → Answerer 流水线。

## 已验证能力

| 能力 | 结果 | 备注 |
| --- | --- | --- |
| DeepSeek + `@tool` | 通过 | 4 条消息，工具被正确调用 |
| 本地 Qwen3-4B（MLX）+ 工具 | 通过 | 7.7 秒，需先声明模型 |
| `encode_messages` / `decode_messages` | 通过 | 833 字符，恢复后仍记得预算 1500 |
| 中途 `steer` | 通过 | 800 字改成一句话生效 |
| `abort` | 通过 | 2 秒内停止，不抛异常 |
| asyncio 下 `await agent.prompt()` | 通过 | 3.3 秒，可放进 FastAPI |
| 工具抛异常 | 通过 | 模型主动说明“天气服务不可用” |
| 10 秒长工具 | 通过 | 无内置超时，需自行 `wait_for` |
| `Hooks.before_tool_call` | 可用 | 可实现“一回合最多一次规划” |

## 三个坑

1. 自定义模型必须用 `provider.model(path, context_window=..., max_tokens=...)` 声明；直接传字符串会报 `ConfigurationError: Unknown model mlx/<path>`。
2. `encode_messages` 返回的是 JSON **字符串**，不是结构体，可直接存 TEXT 字段。
3. Pi 只保存消息，不含行程、证据等业务状态，这些仍需自己的表。

## 复现步骤

```bash
uv venv /tmp/pi-spike/venv
UV_CACHE_DIR=/tmp/uv-cache uv pip install --python /tmp/pi-spike/venv/bin/python pi-python-core
```

- DeepSeek：读 `.env` 里的 `OPENAI_API_KEY`。
- 本地 MLX：先跑 `scripts/serve_local_model.sh`，再用 `OpenAICompletionsProvider(base_url="http://127.0.0.1:8080/v1")`。

## 未验证与风险

- 多轮对话下 4B 的工具调用稳定性（目前只验证了单轮）。
- 长任务取消后的资源回收。
- `pi-python-core` 为单人维护的社区移植，版本迭代快，升级需回归。

## 下一步

P1：规划器输出 `ToolPlan`，用 `ToolDAGScheduler` 执行，替换产品里手写的查询循环。
