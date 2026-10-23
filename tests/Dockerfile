FROM hashicorp/terraform:1.16.6 AS terraform-cli
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
COPY --from=terraform-cli /bin/terraform /usr/local/bin/terraform
COPY app/requirements*.txt ./app/
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r app/requirements-dev.txt && groupadd -g 10001 iacp && useradd -u 10001 -g iacp -M iacp
RUN mkdir /test-tls && chown 10001:10001 /test-tls
COPY app ./app
COPY tests ./tests
COPY docs ./docs
COPY scripts ./scripts
COPY traefik ./traefik
COPY deploy ./deploy
COPY pytest.ini compose*.yaml ./
RUN find /workspace -type d -exec chmod 0755 {} + && find /workspace -type f -exec chmod 0644 {} + \
    && chmod 0755 /workspace/scripts/iacpctl /workspace/scripts/test-certifier-workspace.sh
USER iacp
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]
