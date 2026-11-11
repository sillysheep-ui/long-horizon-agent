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

本地 RL 权重位于 `saved/output/grpo_parser_aligned_run/v8-20260514-140934/checkpoint-150`，是 Hugging Face 格式；该目录可作为后续 MLX 转换的输入，但当前产品层不直接加载它。要使用本地模型，需要先启动提供 `/v1/chat/completions` 的 MLX 服务，再在 `.env` 中设置 `LOCAL_MODEL_BASE_URL` 和 `LOCAL_MODEL_NAME`。API 模式可独立工作。

地点证据优先使用高德地图 POI v5。若地图 Key 不可用，会降级到 DeepSeek Search，返回可点击的网页来源。网页来源只能证明相关页面存在，不代表精确地址或营业状态已经核实；页面会区分“地图 POI”与“网页来源”。两种来源都不可用时，工作流会显示部分完成及失败原因。车票和机票尚未接入产品层，页面不会展示假价格或班次。

## 核心边界

- `model_gateway.py`：模型适配与回退。Agent 职责不等于模型实例，多个角色可以复用一个模型。
- `orchestrator.py`：阶段间传递结构化状态；POI 查询最多并发 4 个；回答中的地点名称和证据 ID 必须匹配查询结果。
- `store.py`：任务版本、状态和事件。新版本会淘汰尚未开始的旧任务，旧版本运行结果不能覆盖新版本。
- `api.py`：任务 API、进度事件和构建后的前端静态文件。

## 验证

```bash
PYTHONPYCACHEPREFIX=/private/tmp/travel-pycache .venv-product/bin/python -m unittest product.test_product -v
cd web && npm run build
```

当前是本地演示阶段。全天在线还需要部署到云服务器、配置域名/HTTPS、持久化卷、访问控制、限流与监控；不能仅凭本地页面可运行就宣称已上线。
