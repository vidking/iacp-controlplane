FROM hashicorp/terraform:1.16.6 AS terraform

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    IACP_TERRAFORM_PATH=/app/terraform \
    IACP_TERRAFORM_MNG_DIR=environments/mng01 \
    IACP_WORKDATA_DIR=/var/lib/iacp-controlplane \
    IACP_MNG_CATALOG=/app/terraform/environments/mng01/catalog.json

RUN groupadd --system iacp && useradd --system --gid iacp --home-dir /app iacp
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY api ./api
COPY worker ./worker
COPY terraform ./terraform
RUN mkdir -p /opt/terraform-plugin-cache /var/lib/iacp-controlplane && \
    cd /app/terraform/environments/mng01 && \
    TF_PLUGIN_CACHE_DIR=/opt/terraform-plugin-cache \
      terraform init -input=false -no-color -backend=false -lockfile=readonly && \
    cd /app/terraform/environments/prx && \
    TF_PLUGIN_CACHE_DIR=/opt/terraform-plugin-cache \
      terraform init -input=false -no-color -backend=false -lockfile=readonly && \
    chown -R iacp:iacp /app /var/lib/iacp-controlplane
USER iacp

EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]

