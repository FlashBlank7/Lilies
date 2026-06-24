ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli curl && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src/agent_platform ./src/agent_platform
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["uvicorn", "agent_platform.api:app", "--host", "0.0.0.0", "--port", "8000"]
