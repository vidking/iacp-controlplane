FROM hashicorp/terraform:1.16.6@sha256:bfffc1ded5fbf0a17beaaf22eb4793980abdb19d0761ff6a3a0efb2ab4d9b316 AS terraform

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    IACP_TERRAFORM_PATH=/app/terraform \
    IACP_TERRAFORM_MNG_DIR=environments/mng01 \
    IACP_WORKDATA_DIR=/var/lib/iacp-controlplane

RUN sed -i \
      -e 's|http://deb.debian.org/debian$|https://snapshot.debian.org/archive/debian/20261104T212339Z|' \
      -e 's|http://deb.debian.org/debian-security$|https://snapshot.debian.org/archive/debian-security/20261104T212339Z|' \
      /etc/apt/sources.list.d/debian.sources && \
    apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 999 iacp && useradd --system --uid 999 --gid iacp --home-dir /app iacp
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY api ./api
COPY worker ./worker
COPY services ./services
COPY terraform ./terraform
RUN mkdir -p /opt/terraform-plugin-cache /var/lib/iacp-controlplane && \
    cd /app/terraform/environments/mng01 && \
    TF_PLUGIN_CACHE_DIR=/opt/terraform-plugin-cache \
      terraform init -input=false -no-color -backend=false -lockfile=readonly && \
    cd /app/terraform/environments/prx && \
    TF_PLUGIN_CACHE_DIR=/opt/terraform-plugin-cache \
      terraform init -input=false -no-color -backend=false -lockfile=readonly && \
    chown -R iacp:iacp /var/lib/iacp-controlplane && \
    chown -R root:root /app /opt/terraform-plugin-cache && \
    chmod -R go-w,a+rX /app /opt/terraform-plugin-cache
USER iacp

EXPOSE 8080
# El código y los proveedores de Terraform son de solo lectura para el usuario
# de runtime; únicamente el volumen de trabajo es escribible.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]

