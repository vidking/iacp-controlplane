FROM hashicorp/terraform:1.16.6 AS terraform

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN groupadd --system iacp && useradd --system --gid iacp --home-dir /app iacp
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    "fastapi==0.115.12" \
    "uvicorn[standard]==0.34.2" \
    "pydantic==2.11.3"
COPY api ./api
COPY worker ./worker
RUN mkdir -p /var/lib/iacp-v2 /workspace/terraform && chown -R iacp:iacp /app /var/lib/iacp-v2 /workspace
USER iacp

EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]

