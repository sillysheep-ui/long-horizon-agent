# 复现说明

这份文档回答一个问题：**别人拿到这个仓库，能复现到什么程度？**

结论先给：

| 目标 | 能否复现 | 需要什么 |
|---|---|---|
| 读懂方法与代码 | 能 | 无 |
| 跑起网页 Demo | 能 | Docker + 一个模型 API Key |
| 重跑产品评测 | 能，分数有漂移 | API Key + 裁判模型 |
| 重跑训练与原始结论 | 不能一键复现 | 训练数据、权重、GPU |

---

## 1. 环境要求

- Python 3.12+（评测与本地运行）
- Node.js 20+（构建前端，Docker 构建会自动完成）
- Docker（可选，推荐用于部署）
- 网络：需要能访问模型 API；Docker 镜像源视网络环境而定

本地模型（可选）额外需要：

- macOS + Apple Silicon + MLX（`scripts/serve_local_model.sh`）
- Qwen3-4B 合并权重（约 7.5GB，需自行准备）

> 镜像默认按 `linux/arm64` 构建。在 x86 机器上请先 `docker buildx build --platform linux/amd64 ...` 重新构建。

---

## 2. 复现产品 Demo

```bash
git clone <repo> && cd long-horizon-agent
cp .env.example .env
# 至少填写 OPENAI_API_KEY；如需指定模型，同时填 OPENAI_BASE_URL 与 LLM_MODEL_ID
docker compose up -d --build
open http://localhost:8000
```

没有 Docker 时也可以原生运行：

```bash
pip install -r product/requirements.txt
uvicorn product.api:app --host 0.0.0.0 --port 8000
```

最小可用条件：

- `OPENAI_API_KEY`：必需，否则规划与回答阶段直接失败
- `AMAP_MAPS_API_KEY`：可选。不配置时会降级为网页搜索，地点名称与地址质量下降
- `TRAVEL_DB_PATH`：可选，默认 `./data/agent.db`

Docker 相关的完整说明见 [`DEPLOY.md`](DEPLOY.md)。

---

## 3. 复现产品评测

```bash
export $(grep -v '^#' .env | xargs)      # 或手动 source
python3 -m evaluation.product_eval --profile api_only --split test \
  --judge-model deepseek-v4-flash --seed 0
```

- `--split dev|test|holdout`：日常迭代用 `dev`（12 条），对外报告用 `test`（100 条），`holdout`（40 条）只在发布前跑
- `--seed`：写入报告，便于对齐
- 裁判模型会写进 `report.md`；换裁判等于换尺子，不要跨模型比较分数
- 典型耗时：`test` 100 条约 30–60 分钟（含裁判）

多轮修改评测：

```bash
python3 -m evaluation.revision_eval --profile api_only --seed 0
```

结果写到 `results/product_eval/<时间戳>-<profile>/`，包含 `report.md` 与 `records.json`。

### 分数会漂移

以下因素会让同一份代码跑出不同分数：

- 裁判模型或服务端版本变化
- 工具返回的实时网页内容变化（网页搜索没有固定快照）
- 高德 Key 是否有效（有效时走 POI，否则降级网页搜索）
- 模型服务的非确定性（即使 `temperature=0`）

因此建议看**区间与趋势**，不要拿单次分数做精确对比。评测指标的定义见 [`PRODUCT_EVAL.md`](PRODUCT_EVAL.md)。

---

## 4. 复现训练（需要额外资源）

仓库**不包含**以下内容：

- 训练数据（教师轨迹、SFT/GRPO 样本）
- 模型权重（SFT LoRA、GRPO checkpoint）
- 运行产物（数据库、日志、评测输出）

仓库包含的是构造脚本与训练脚本：

```bash
bash scripts/train_sft.sh      # SFT 冷启动
bash scripts/train_rl.sh       # 多轮 GRPO
```

要真正重跑训练，还需要：

- GPU（多卡）+ vLLM 0.11 + DeepSpeed + ms-swift 环境
- 自备数据采集所需的工具 Key（FireCrawl、Tavily、SerpAPI 等）
- 自行生成或申请训练数据

因此「训练结果可复现」这一条，目前只能做到**方法可复现**，做不到**数值可复现**。

---

## 5. 数据可用性

**公开在仓库中：**

- 全部代码
- 评测用例：`evaluation/product_cases.json`（152 条）、`evaluation/product_revision_cases.json`（10 条）
- 原始协议评测结果：`results/original_protocol/`（8 个 jsonl / summary）

**不在仓库中：**

| 内容 | 原因 |
|---|---|
| 训练数据与教师轨迹 | 体积大；含第三方网页内容与工具调用记录，分发权限不明确 |
| 模型权重（约 7.5GB） | 超出 Git 仓库合理体积，需单独分发 |
| `.env` 与各类 Key | 安全 |
| 运行数据库与日志 | 属于运行产物，可重新生成 |

如需训练数据或权重用于研究复现，可通过 issue 联系作者说明用途。

---

## 6. 快速自检

```bash
python3 -m pytest tests -q
```

全绿说明代码与环境基本正常；若失败，先看 `docs/DEPLOY.md` 的环境要求。
