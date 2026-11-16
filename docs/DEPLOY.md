# Docker 部署

把产品层（FastAPI + 前端静态页）打成一个镜像，运行在 8000 端口。

## 1. 准备

```bash
cp .env.example .env        # 填入 DEEPSEEK_API_KEY / AMAP_MAPS_API_KEY 等
```

容器里跑不了本地 MLX 权重，默认按 `MODEL_PROFILE=api_only` 走 DeepSeek；
如果坚持用本地模型，见文末“本地模型”。

## 2. 构建与运行

**方式 A：docker compose（推荐）**

```bash
docker compose up -d --build
```

**方式 B：docker 命令**

```bash
docker build -t long-horizon-agent:latest .
docker run -d --name travel-agent \
  -p 8000:8000 \
  --env-file .env \
  -e MODEL_PROFILE=api_only \
  -v "$(pwd)/data:/app/data" \
  long-horizon-agent:latest
```

`data/` 会挂到容器里的 `/app/data`，SQLite 文件 `agent.db` 落在宿主机上，删容器不丢数据。

## 3. 验证

```bash
curl -s localhost:8000/api/health        # {"status":"ok",...}
open http://localhost:8000               # 浏览器打开对话界面
docker logs -f travel-agent              # 看阶段日志
```

## 4. 停止与清理

```bash
docker compose down            # 停容器，保留 data/
docker compose down --rmi local -v   # 连镜像一起删
```

## 5. 没有 Docker Desktop 的 macOS（Lima 方案）

macOS 上 Docker 需要一台 Linux 虚拟机。如果不想装 Docker Desktop（需要管理员权限、且
ghcr.io 与 Docker Hub 在部分网络下不可达），可以用 Lima 手工起一台，再用里面的 Docker：

```bash
brew install lima            # 或用 GitHub Release 的 lima-*-Darwin-arm64.tar.gz
mkdir -p ~/.lima-images
curl -fL -o ~/.lima-images/noble-server-cloudimg-arm64.img \
  https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cloud-images/noble/current/noble-server-cloudimg-arm64.img
# 把 deploy/lima.yaml 里的 images.location 改成上面的绝对路径
limactl start deploy/lima.yaml --name=app --tty=false
limactl shell app -- sudo docker version
```

之后所有 Docker 命令都套一层：

```bash
limactl shell app -- sudo docker build -t long-horizon-agent:local ~/long-horizon-agent
limactl shell app -- sudo docker run -d --name app -p 8000:8000 \
  --env-file ~/long-horizon-agent/.env \
  -e TRAVEL_DB_PATH=/app/data/agent.db \
  -v ~/long-horizon-agent/data:/app/data \
  long-horizon-agent:local
```

Lima 会把虚拟机里监听 8000 的端口转发到宿主机 `127.0.0.1:8000`。

## 6. 本地模型（可选）

容器内没有 MLX，也没有 7.5G 权重。若要用 `MODEL_PROFILE=local_first`：

- 宿主机先启动 `scripts/serve_local_model.sh`
- 容器里把 `LOCAL_MODEL_BASE_URL` 指到宿主机：

```bash
docker run ... -e LOCAL_MODEL_BASE_URL=http://host.docker.internal:8080/v1 \
  -e LOCAL_MODEL_NAME=/Users/sheep/.../checkpoint-150 \
  long-horizon-agent:latest
```

注意 macOS 上 Docker Desktop 才自带 `host.docker.internal`；colima 需要加 `--add-host` 或改用宿主机局域网 IP。

## 7. 已知限制

- 镜像只包含 `product/` 与 `inference/`，评测脚本、训练脚本不进镜像。
- 没有鉴权、限流和 HTTPS；要公网访问请放在反向代理后面。
- 首次 `docker build` 需要拉 Node、Python 基础镜像，约 1–2 GB。
