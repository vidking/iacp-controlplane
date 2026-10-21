FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
COPY app/requirements*.txt ./app/
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r app/requirements-dev.txt && groupadd -g 10001 iacp && useradd -u 10001 -g iacp -M iacp
COPY app ./app
COPY tests ./tests
COPY docs ./docs
COPY scripts ./scripts
COPY traefik ./traefik
COPY pytest.ini compose*.yaml ./
RUN mkdir /test-tls && chown 10001:10001 /test-tls
USER iacp
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]
