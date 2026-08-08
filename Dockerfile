# Lilies 后端（FastAPI + SQLite）
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY platform/backend ./platform/backend
RUN pip install --no-cache-dir .

# 数据全部落在 /app/data（挂卷持久化）：SQLite、事件冷文件、构建转录、密钥
ENV DATA_DIR=/app/data \
    WORKSPACE_ROOT=/app/workspaces \
    PYTHONUNBUFFERED=1
VOLUME ["/app/data", "/app/workspaces"]

EXPOSE 8001
CMD ["uvicorn", "agent_platform.api:app", "--host", "0.0.0.0", "--port", "8001"]
