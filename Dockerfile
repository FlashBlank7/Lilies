# Lilies 后端（FastAPI + SQLite）
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY platform/backend ./platform/backend
RUN pip install --no-cache-dir .

# 数据全部落在 /app/data（挂卷持久化）：SQLite、事件冷文件、构建转录、密钥
# 端口交给应用自己的配置（API_PORT/PORT，默认 8000），别在这里另写一份：
# 曾经这里硬写 8001 而 compose 映射并健康检查 8000，容器永远不健康。
# 容器里必须监听 0.0.0.0——应用默认 127.0.0.1 是给本机开发用的。
ENV DATA_DIR=/app/data \
    WORKSPACE_ROOT=/app/workspaces \
    API_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1
VOLUME ["/app/data", "/app/workspaces"]

EXPOSE 8000
CMD ["agent-platform"]
