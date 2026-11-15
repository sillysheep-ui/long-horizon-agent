# 多阶段构建：先用 Node 构建前端，再放进 Python 运行时。
# 使用方式见 docs/DEPLOY.md。

FROM node:20-bookworm-slim AS web-builder
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY product/requirements.txt product/requirements.txt
RUN pip install --no-cache-dir -r product/requirements.txt

COPY product ./product
COPY inference ./inference
COPY --from=web-builder /web/dist ./web/dist

RUN mkdir -p /app/data && chown -R app:app /app
USER app

ENV TRAVEL_DB_PATH=/app/data/agent.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "product.api:app", "--host", "0.0.0.0", "--port", "8000"]
