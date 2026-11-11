# 旅行 Agent 产品层（本地演示）

该目录是现有训练、评测代码之外的独立在线推理层。浏览器提交自然语言需求，后端保存任务并调度需求、规划、查询、证据核对、回答五个阶段。任务和事件保存在 SQLite，页面关闭后仍可查看结果并继续修改。

## 启动

在仓库根目录执行：

```bash
uv venv .venv-product
uv pip install --python .venv-product/bin/python -r product/requirements.txt
cd web && npm install && npm run build && cd ..
.venv-product/bin/python -m uvicorn product.api:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000`。开发时可以另开终端在 `web` 目录运行 `npm run dev`，浏览器访问 `http://127.0.0.1:5173`。

## 配置

后端读取仓库根目录 `.env`。模型路由依次支持本地 OpenAI 兼容服务、DeepSeek、任意 OpenAI 兼容 API。`MODEL_PROFILE` 可设为 `local_first`、`api_first` 或 `api_only`。如果未显式设置 `LOCAL_MODEL_BASE_URL` 且已有 API Key，会先尝试 API。仓库旧的 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 配置可复用：若地址是 DeepSeek，会自动选用 `deepseek-flash`，避免把旧的其他厂商模型名发送给 DeepSeek。

本地 RL 权重位于 `saved/output/grpo_parser_aligned_run/v8-20260514-140934/checkpoint-150`，是 Hugging Face 格式，可由 MLX 服务直接加载，见下文「本地模型」。未启动本地服务时，产品层会回落到 DeepSeek；API 模式可独立工作。

地点证据优先使用高德地图 POI v5。若地图 Key 不可用，会降级到 DeepSeek Search，返回可点击的网页来源。网页来源只能证明相关页面存在，不代表精确地址或营业状态已经核实；页面会区分“地图 POI”与“网页来源”。两种来源都不可用时，工作流会显示部分完成及失败原因。车票和机票尚未接入产品层，页面不会展示假价格或班次。

## 本地模型

`MODEL_PROFILE=local_first` 时，产品层先连本地 OpenAI 兼容服务，失败或超时才走 DeepSeek。启动本地 Qwen3-4B：

```bash
# 1. 起 MLX 服务（首次会自动建 .venv-local 并安装 mlx-lm）
scripts/serve_local_model.sh /path/to/Qwen3-4B-checkpoint

# 2. 让产品层走本地模型
export MODEL_PROFILE=local_first
export LOCAL_MODEL_BASE_URL=http://127.0.0.1:8080/v1
export LOCAL_MODEL_NAME=/path/to/Qwen3-4B-checkpoint   # 必须与服务端加载的模型一致
python3 -m uvicorn product.api:app --port 8000
```

注意：

- `LOCAL_MODEL_NAME` 不能随便起别名。mlx-lm 会把未加载的名字当作 HuggingFace 仓库去下载，返回 404；填启动脚本传入的同一路径即可。
- 服务启动后第一次请求要编译计算图，可能超过网关 40 秒超时并静默回落到 DeepSeek。先 `curl` 一句短请求预热，可避免第一段用错模型。
- 实测（Apple Silicon 24G，bf16）：一句完整行程约 100 秒，DeepSeek 约 9 秒；4B 会直接拿网页片段当行程标题，质量低于 DeepSeek。

## 核心边界

- `model_gateway.py`：模型适配与回退。Agent 职责不等于模型实例，多个角色可以复用一个模型。
- `orchestrator.py`：阶段间传递结构化状态；POI 查询最多并发 4 个；回答中的地点名称和证据 ID 必须匹配查询结果。
- `store.py`：任务版本、状态和事件。新版本会淘汰尚未开始的旧任务，旧版本运行结果不能覆盖新版本。
- `api.py`：任务 API、进度事件和构建后的前端静态文件。

## 证据来源标记

`provenance.py` 统一了每条查询结果的字段，页面按同一套规则展示：

| 字段 | 含义 | 取值 |
| --- | --- | --- |
| `source` | 来源名称 | 高德地图 POI / DeepSeek 网页搜索 / 模拟数据 |
| `source_kind` | 来源类型 | `map_poi` / `web_search` / `simulated` / `none` |
| `status` | 查询结果 | `ok` / `empty` / `failed` / `unavailable` |
| `fetched_at` | 查询时间（UTC ISO） | 历史记录缺失时为空，页面显示“查询时间未知” |
| `simulated` | 是否模拟数据 | 布尔值；为真时页面显示“模拟数据” |
| `verification_level` | 核实程度 | `address_verified`（地图 POI）/ `source_linked`（仅网页来源）/ `unverified` / `none` |

模拟数据永远不会标成“已核实”；查询失败或服务不可用时保留原因，不展示地址或价格。

## 验证

```bash
PYTHONPYCACHEPREFIX=/private/tmp/travel-pycache .venv-product/bin/python -m unittest product.test_product -v
cd web && npm run build
```

当前是本地演示阶段。全天在线还需要部署到云服务器、配置域名/HTTPS、持久化卷、访问控制、限流与监控；不能仅凭本地页面可运行就宣称已上线。
