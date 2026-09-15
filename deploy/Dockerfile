# ─────────────────────────────────────────────────────────────────────
# ai-company (狗蛋儿) 服务端镜像 —— 供管理端「民意智感中心」嵌入式调用
#
# 构建（在仓库根目录，Mac 上要指定 linux/amd64，因为目标机是 x86_64）：
#   docker buildx build --platform linux/amd64 -f deploy/Dockerfile \
#     -t crpi-bqbwg1s59o0fx9ln.cn-beijing.personal.cr.aliyuncs.com/letter/ai-company:latest .
#
# 运行：
#   docker run -d --name ai-company --restart unless-stopped \
#     -p 127.0.0.1:8020:8020 \
#     --env-file /home/my/ai-company/ai-company.env \
#     -v /home/my/ai-company/data:/data \
#     crpi-.../letter/ai-company:latest
# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# HOME=/data：ai-company 的持久化目录（~/.ai-company/...）会落在挂载卷里，
# 这样 multiuser.db / service_sessions / engram_memory.db 容器重建也不丢。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    HOME=/data \
    AI_COMPANY_HOME=/app \
    AI_COMPANY_SERVICE_MODE=1 \
    AI_COMPANY_API_HOST=0.0.0.0 \
    AI_COMPANY_API_PORT=8020

WORKDIR /app

# ── 第 1 层：依赖（源码改动不会让这一层缓存失效）──────────────────────
COPY deploy/requirements.docker.txt /tmp/requirements.docker.txt
COPY deploy/vendor/engram-router /tmp/vendor/engram-router
RUN pip install --no-cache-dir -r /tmp/requirements.docker.txt \
 && pip install --no-cache-dir /tmp/vendor/engram-router \
 && python -c "import engram_router, fastapi, uvicorn, langgraph, langchain_openai; print('deps ok')"

# ── 第 2 层：源码（.dockerignore 已排除 .env / .git / 测试产物）────────
COPY . /app

# 数据卷（挂载点；不挂也能跑，只是数据随容器销毁）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8020

HEALTHCHECK --interval=30s --timeout=6s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8020/ai/health', timeout=5).status==200 else 1)"

CMD ["python", "-m", "src.api_server", "--host", "0.0.0.0", "--port", "8020"]
