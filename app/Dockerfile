FROM hashicorp/terraform:1.16.6 AS terraform-cli
FROM python:3.12-slim AS application
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 10001 iacp && useradd -u 10001 -g iacp -M iacp
COPY iacp ./iacp
COPY alembic.ini .
COPY alembic ./alembic
# Normalizar el checkout sin conceder escritura al usuario de ejecución.
RUN chown -R root:root /app \
    && find /app -type d -exec chmod 0755 {} + \
    && find /app -type f -exec chmod 0644 {} +
USER iacp
EXPOSE 8000
CMD ["uvicorn", "iacp.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]

FROM application AS terraform-worker
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/lib/iacp/terraform && chown 10001:10001 /var/lib/iacp/terraform \
    && chmod 0700 /var/lib/iacp/terraform
COPY --from=terraform-cli /bin/terraform /usr/local/bin/terraform
USER iacp
CMD ["celery", "-A", "iacp.terraform.worker:celery_app", "worker", "-Q", "terraform", "--loglevel=INFO", "--concurrency=1", "--without-gossip", "--without-mingle"]

# El build predeterminado conserva la API y no incorpora Terraform ni Git.
FROM application AS api
