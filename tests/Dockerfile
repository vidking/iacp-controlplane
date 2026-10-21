FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
COPY app/requirements*.txt ./app/
RUN pip install --no-cache-dir -r app/requirements-dev.txt && groupadd -g 10001 iacp && useradd -u 10001 -g iacp -M iacp
COPY app ./app
COPY tests ./tests
COPY docs ./docs
COPY scripts ./scripts
COPY pytest.ini compose*.yaml ./
RUN find /workspace -type d -exec chmod 0755 {} + && find /workspace -type f -exec chmod 0644 {} +
USER iacp
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]
